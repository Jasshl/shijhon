"""Catalog additions to search results and artist pages.

``search3`` (JSON and XML): Navidrome's answer comes first, unchanged and in its
order; catalog artists, albums and songs fill what is left of each requested count, on
the first page (offset 0) of each type only. Among the additions an exact artist match
comes first, and that artist's albums before other artists' singles. Nothing is added to an
empty query (clients use it for full-library syncs), to a query shorter than the minimum
length, to JSONP or HEAD requests, or when the catalog does not answer within the budget:
then Navidrome's answer is passed on as it came.

``getArtist`` (JSON and XML) for an artist in the library: the catalog releases of the
artist of that name that the library does not have are added after Navidrome's albums.
Discographies are saved: a saved one answers at once and is refreshed in the
background when old; only an artist without one waits for the catalog. A client opening
many artist pages without saved discographies in a short time (a sync) gets the library's
pages alone.

De-duplication against the library: catalog items that are in the library (committed or
filled), albums that are editions of a library album in the answer (the edition rules of
``catalog/editions.py``), songs with an owned song's ISRC or with its title, artist
and length, and artists with an owned artist's name. The catalog's search and artist
answers are cached and coalesced (``catalog/cache.py``), so search-as-you-type does not
become per-keystroke catalog traffic. Catalog items credited to a library artist link
to that artist.

Every ``search3`` is logged without its text: its length, the requested counts and
offsets, and what the catalog did. A client sending many different searches in a short
time gets the library's answers alone for a while (the search guard), and a client's
newer search ends the older one's wait for the catalog. Song-only searches (no artists
or albums asked for: a client's "Tracks" search, or a client looking up each track of an
album it opened) are kept apart from the others: an isolated one gets additions, but
their bursts - a few different ones within a fraction of a second - trip a guard of their
own at once and get the library's answers (an advanced setting turns their additions
off altogether); they have one
catalog lookup of their own at a time (their failures do not pause the others'
additions), they never end the wait of a search of the other kind, and the albums they
find are not queued for matching (they are lookups, not answers a listener sees).

``getTopSongs`` (JSON and XML) of an artist without songs in the library:
the catalog's top songs of that artist, as catalog songs
like search results, up to the requested count (the catalog's first 10). The request
names the artist; the name is the catalog artist shown most recently with exactly that
name (a search result, an artist page), else the first exact match of a catalog search.
A catalog artist ID (Navidrome 0.64.2 also takes ``id``) is that artist, or - one the
library has - the library artist's. An artist with songs in the library gets Navidrome's
answer. It is a view: top songs are saved like discographies, only an artist without
saved ones waits for the catalog (the same budget), nothing is asked while the client
walks artist pages or trips the search guard, and a catalog that fails or is too slow
leaves Navidrome's answer (its empty one).

Credentials are checked before any catalog call.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import replace
from typing import Any, TypeVar

import anyio

from shijhon.catalog.artwork import ArtworkIndex
from shijhon.catalog.base import (
    Catalog,
    CatalogError,
    SearchResults,
    addresses_for_clients,
)
from shijhon.catalog.base import scope as catalog_scope
from shijhon.catalog.editions import LibraryAlbum, dedupe_editions, merge_twins
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    Twins,
)
from shijhon.locks import KeyedLocks
from shijhon.matching.normalize import (
    artist_names,
    featured_names,
    fold,
    title_key,
)
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.proxy.app import Forward, Handler, HandlerResult, RequestContext
from shijhon.proxy.params import RestCall
from shijhon.proxy.responses import subsonic_ok
from shijhon.proxy.upstream import Upstream
from shijhon.store import Store
from shijhon.views.answers import LibraryAnswer, accepts_gzip, library_answer
from shijhon.views.bursts import Burst, Bursts, Client
from shijhon.views.covers import CoverPrefetch
from shijhon.views.discographies import Discographies
from shijhon.views.entries import album_entry, artist_entry, each, song_entry
from shijhon.views.ids import CatalogId, artist_id
from shijhon.views.library_artists import LibraryArtists
from shijhon.views.library_songs import LibrarySongs, Unknown
from shijhon.views.library_songs import isrcs as _isrcs
from shijhon.views.library_songs import same_recording as _same_recording
from shijhon.views.library_songs import same_song as _same_song
from shijhon.views.shown import ShownArtists

log = logging.getLogger(__name__)
T = TypeVar("T")
Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], None]

SEARCH_LIMIT = 25  # catalog results per type: one cached answer serves every client
MAX_COUNT = 500  # larger requested counts are syncs, not searches: no additions
MAX_PENDING = 4  # catalog lookups for additions running at a time
ARTIST_BUDGET = 1.0  # seconds a search answer waits for its entries' artist items
# Song-only searches' lookups at a time, apart from the others: bursts no longer
# reach them (their settle and guard), and a typed "Tracks" query's superseded lookups
# go on for the cache while the newest one needs a slot of its own.
SONG_LOOKUPS = 4
TOP_SONGS = 10  # an artist's top songs asked of the catalog and saved (one request)
TOP_COUNT = 50  # getTopSongs' count when a client names none (Navidrome's default)
NAME_SECONDS = 600.0  # how long a library artist's name, asked by its ID, is remembered
NAMES_REMEMBERED = 5000
_TYPES = (
    ("artist", "artistCount", "artistOffset"),
    ("album", "albumCount", "albumOffset"),
    ("song", "songCount", "songOffset"),
)
_SPACES = re.compile(r"\s+")
# Artist names that stand for no one artist: their pages get no additions.
_NOBODY = {"variousartists", "various", "va", "unknownartist", "unknown", "soundtrack"}


class SearchReport:
    """One ``search3`` for the instance log: never its text, only its length, the requested
    counts and offsets, what the catalog did, and what was added."""

    def __init__(self, call: RestCall) -> None:
        self.client = _printable(call.client) or "?"
        self.length = 0
        self.counts = ", ".join(
            f"{kind} {_number(call.get(count), 20)}@{_number(call.get(offset), 0)}"
            for kind, count, offset in _TYPES
        )
        self.catalog = "not asked"
        self.added: dict[str, int] = {}
        self.started = time.perf_counter()

    def skipped(self, why: str) -> HandlerResult:
        """Navidrome answers alone."""
        self.catalog = f"skipped: {why}"
        return None

    def __str__(self) -> str:
        query = f"{self.length} characters" if self.length else "empty query"
        added = "/".join(str(self.added.get(kind, 0)) for kind, _, _ in _TYPES)
        return (
            f"search3 c={self.client}: {query}; {self.counts}; catalog {self.catalog};"
            f" added {added} (artists/albums/songs); {time.perf_counter() - self.started:.2f}s"
        )


class TopSongsReport:
    """One ``getTopSongs`` for the instance log: never the artist's name."""

    def __init__(self, call: RestCall, *, by_id: bool) -> None:
        self.client = _printable(call.client) or "?"
        self.by_id = by_id
        self.library = "not checked"
        self.catalog = "not asked"
        self.own = 0  # Navidrome's own top songs
        self.songs = 0  # the catalog's, added
        self.native = 0  # ... of them the library's own entries
        self.started = time.perf_counter()

    def __str__(self) -> str:
        return (
            f"getTopSongs c={self.client}: an artist {'ID' if self.by_id else 'name'};"
            f" in the library: {self.library}; Navidrome's own: {self.own};"
            f" catalog {self.catalog}; {self.songs} of the catalog's songs added"
            f" ({self.native} as the library's); {time.perf_counter() - self.started:.2f}s"
        )


class CatalogAdditions:
    def __init__(
        self,
        catalog: Catalog | None,
        store: Store,
        upstream: Upstream,
        *,
        navidrome: NavidromeService | None = None,
        min_query_length: int = 3,
        budget_seconds: float = 8.0,
        artist_pages: bool = True,
        library_id: int = 1,
        spawn: Spawn | None = None,
        rest_seconds: float = 30.0,
        cache_seconds: float = 3600.0,
        library_artists: LibraryArtists | None = None,
        discographies: Discographies | None = None,
        artist_sync: Bursts | None = None,
        search_guard: Bursts | None = None,
        song_search_guard: Bursts | None = None,
        song_rate_guard: Bursts | None = None,
        twins: Twins = Twins.EXPLICIT,
        song_only_additions: bool = True,
        song_settle_seconds: float = 0.15,
        artwork: ArtworkIndex | None = None,
        top_sync: Bursts | None = None,
        shown: ShownArtists | None = None,
        server: Callable[[], dict[str, object]] = dict,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.catalog = catalog
        # Saved discographies' artwork is noted here, for their covers.
        self.artwork = artwork
        self.store = store
        self.upstream = upstream
        self.navidrome = navidrome  # service account
        self.min_query_length = max(1, min_query_length)
        self.budget = budget_seconds
        self.artist_pages = artist_pages
        self.library_id = library_id
        self.spawn = spawn  # background work for the app's lifetime
        self.rest_seconds = rest_seconds
        self.cache_seconds = cache_seconds  # how long the catalog's cache keeps an answer
        self.library_artists = library_artists
        self.discographies = discographies
        self.artist_sync = artist_sync
        # Top songs of artists not in the library, many in a short time: a client walking
        # artist pages (their own count, with the artist pages' settings).
        self.top_sync = top_sync
        # The catalog artists clients were shown, by name (getTopSongs names artists).
        self.shown = shown
        # Which of the catalog's songs the library has (top songs).
        self.library_songs = LibrarySongs(store, navidrome, upstream, clock=clock)
        self.server = server  # Navidrome's envelope fields
        self.search_guard = search_guard
        self.song_search_guard = song_search_guard  # song-only searches' bursts
        # Song-only searches sent one at a time, many in a while: the search guard's
        # settings, apart from the other searches.
        self.song_rate_guard = song_rate_guard
        # Each client's song-only term a newer one extends or shortens (typing): one term.
        self._song_terms: dict[Client, str] = {}
        self.twins = twins  # clean/explicit twins: which to show
        # Song-only searches get additions when isolated; off: never. They
        # wait a moment first: the next search of a burst supersedes them meanwhile.
        self.song_only_additions = song_only_additions
        self.song_settle = song_settle_seconds
        # Owned albums a search result or an artist page shows: matched and filled in the
        # background, when set.
        self.exposed: Callable[[Client, Iterable[str]], None] | None = None
        self.clock = clock
        # After the catalog failed (unavailable, rate limited), additions pause until then.
        self.resting_until = 0.0
        # Catalog lookups for additions still running, by what they look up; the ones
        # started by song-only searches.
        self._running: dict[str, tuple[anyio.Event, list[Any]]] = {}
        self._song_lookups: set[str] = set()
        # When each lookup last succeeded: the catalog's cache answers it for a while.
        self._answered: dict[str, float] = {}
        # Library artists' names by their IDs, asked of Navidrome for top songs (guests on a
        # song: not in the index of album artists): one lookup at a time for an ID.
        self._naming = KeyedLocks()
        self._names: dict[str, tuple[str, float]] = {}
        # Each client's latest search of each kind (song-only or not): its term, and an
        # event that ends its wait.
        self._latest: dict[tuple[Client, bool], tuple[str, anyio.Event]] = {}
        self.catalog_searches = 0  # observable in tests: searches that reached the catalog
        # The covers of the first catalog items of an answer, fetched ahead.
        self.prefetch: CoverPrefetch | None = None

    def handlers(
        self, get_artist: Handler | None = None, ids: Handler | None = None
    ) -> dict[str, Handler]:
        """``search3``, and ``getArtist``: library artists get the additions; a catalog
        artist opens as the library artist of that name if there is one, else as
        ``get_artist`` (the virtual view). ``getTopSongs``; a request it leaves alone goes
        to ``ids`` (catalog IDs in any method)."""
        top_songs = _unbroken(self.top_songs)

        async def top(call: RestCall, ctx: RequestContext) -> HandlerResult:
            found = await top_songs(call, ctx)
            if found is None and ids is not None:
                return await ids(call, ctx)
            return found

        views: dict[str, Handler] = {"search3": _unbroken(self.search3), "getTopSongs": top}
        if self.artist_pages:
            library_page = _unbroken(self.artist_page)

            async def artist(call: RestCall, ctx: RequestContext) -> HandlerResult:
                if CatalogId.parse(call.get("id")) is None:
                    return await library_page(call, ctx)
                try:
                    found = await self.catalog_artist_page(call, ctx)
                except Exception as exc:
                    log.warning("catalog artist page failed: %s", type(exc).__name__)
                    found = None
                if found is not None:
                    return found
                return await get_artist(call, ctx) if get_artist is not None else None

            views["getArtist"] = artist
        return views

    # --- search3 ---------------------------------------------------------------------------

    async def search3(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        catalog = self.catalog
        if catalog is None:
            return None
        report = SearchReport(call)
        try:
            return await self._search3(call, ctx, catalog, report)
        finally:
            log.info("%s", report)

    async def _search3(
        self, call: RestCall, ctx: RequestContext, catalog: Catalog, report: SearchReport
    ) -> HandlerResult:
        term = _term(call.get("query"))
        report.length = len(term)
        if call.fmt == "jsonp" or call.http_method == "HEAD":
            return report.skipped("JSONP" if call.fmt == "jsonp" else "HEAD")
        if not term:
            return report.skipped("empty query")  # clients' full-library syncs
        if len(term) < self.min_query_length:
            return report.skipped("short query")
        wanted = _wanted(call)
        if not wanted:
            return report.skipped("sync-sized or later pages")
        songs_only = set(wanted) == {"song"}
        if songs_only and not self.song_only_additions:
            return report.skipped("song-only search")  # off: the library answers
        if not self._this_library(call):
            return report.skipped("another music folder")
        # Before Navidrome is asked for the library's answer: credentials it refuses reach
        # it once, not twice (its login limit counts each failure).
        caller = await ctx.caller()
        if caller is None:
            return report.skipped("credentials")  # Navidrome's refusal, or its own answer
        client = (caller.username, call.client)
        results: list[SearchResults] = []
        answers: list[LibraryAnswer | None] = []

        async def library(stop: anyio.CancelScope) -> None:
            answer = await self._navidrome(call)
            answers.append(answer)
            found = await answer.parsed("searchResult3") if answer is not None else None
            if found is None or not _room(found[1], wanted):
                if report.catalog == "not asked":
                    report.catalog = "not needed"
                stop.cancel()  # nothing can be added: no need to wait for the catalog

        async def search() -> None:
            if self._guarded(client, term, songs_only):
                report.catalog = "skipped: " + _guard_name(songs_only)
                return
            same = self._latest.get((client, songs_only), ("", None))[0] == term
            newer = self._newest(client, term, songs_only)
            if songs_only and self.song_settle > 0 and not same:
                with anyio.move_on_after(self.song_settle):
                    await newer.wait()
                if newer.is_set():
                    report.catalog = "superseded"  # the next search of a burst came
                    return
                guards = (self.song_search_guard, self.song_rate_guard)
                if any(g is not None and g.bursting(client) for g in guards):  # tripped
                    report.catalog = "skipped: " + _guard_name(songs_only)
                    return
            self.catalog_searches += 1
            found, report.catalog = await self._within_budget(
                f"search:{term}",
                lambda: catalog.search(term, SEARCH_LIMIT),
                stop=newer,
                songs_only=songs_only,
            )
            if found is not None:
                results.append(found)

        async with anyio.create_task_group() as tg:
            tg.start_soon(library, tg.cancel_scope)
            tg.start_soon(search)
        answer = answers[0] if answers else None
        if answer is None:
            return None
        compress = accepts_gzip(call)  # as Navidrome compresses its answers
        found = await answer.parsed("searchResult3")
        if found is not None and not songs_only:  # a track lookup shows the listener nothing
            await self._expose_search(call, ctx, found[1])
        if found is None or not results:
            return answer.reply(compress=compress)
        document, library_results = found
        try:
            additions = await self._search_additions(term, wanted, library_results, results[0])
        except Exception as exc:  # Navidrome's answer, rather than asking it again
            log.warning("search additions failed: %s", type(exc).__name__)
            return answer.reply(compress=compress)
        report.added = {kind: len(entries) for kind, entries in additions.items()}
        if not any(additions.values()):
            return answer.reply(compress=compress)
        for kind, entries in additions.items():
            if entries:
                library_results[kind] = [*library_results.get(kind, []), *entries]
        if self.prefetch is not None and not songs_only:
            shown = [e for kind in ("artist", "album", "song") for e in additions.get(kind, [])]
            self.prefetch.page(client, shown)
        if self.shown is not None and additions.get("artist"):  # by name, for getTopSongs
            added = {e["id"] for e in additions["artist"]}
            self.shown.shown(a for a in results[0].artists if artist_id(a.ref) in added)
        return answer.reply(document=document, compress=compress)

    def _guarded(self, client: Client, term: str, songs_only: bool) -> bool:
        """The search guard: a client sending many different searches in a short time gets
        the library's answers alone for a while. Song-only searches have guards of their
        own, so a client's burst of them leaves its other searches alone: one for
        bursts, where a term extending or shortening the previous one is the same
        search (typing), and one with the search guard's settings for song-only searches
        sent one at a time. When one trips, the client's song-only search waiting for the
        catalog is answered from the library too."""
        if not songs_only:
            return self._note(self.search_guard, client, term, songs_only)
        what = self._song_term(client, term)
        tripped = [self._note(g, client, what, True) for g in (self.song_search_guard,
                                                               self.song_rate_guard)]  # fmt: skip
        if any(tripped):
            waiting = self._latest.get((client, True))
            if waiting is not None:
                waiting[1].set()
        return any(tripped)

    def _song_term(self, client: Client, term: str) -> str:
        """The song-only search a term belongs to: the client's previous one when it only
        extends or shortens it (typing), else itself."""
        previous = self._song_terms.get(client)
        if previous is not None and (term.startswith(previous) or previous.startswith(term)):
            return previous
        if len(self._song_terms) > 1024:
            self._song_terms.clear()
        self._song_terms[client] = term
        return term

    def _note(self, guard: Bursts | None, client: Client, what: str, songs_only: bool) -> bool:
        if guard is None:
            return False
        burst = guard.note(client, what)
        if burst is Burst.STARTED:
            log.info(
                "%s: %s sent %d different %s within %gs;"
                " answering those from the library alone for at least %gs",
                _guard_name(songs_only),
                _printable(client[1]) or "a client",
                guard.count(client),
                "song-only searches" if songs_only else "searches",
                guard.window,
                max(guard.pause, guard.window),
            )
        return burst is not Burst.NO

    def _newest(self, client: Client, term: str, songs_only: bool) -> anyio.Event:
        """This search is the client's newest of its kind: an older one with another term
        stops waiting for the catalog (its lookup goes on for the cache). The event is set
        when a newer search arrives. Song-only searches and the others are kept apart: a
        client's track lookups never end the wait of a search the listener typed."""
        key = (client, songs_only)
        previous = self._latest.get(key)
        if previous is not None and previous[0] == term:
            return previous[1]
        if previous is not None:
            previous[1].set()
        if len(self._latest) > 1024:
            self._latest.clear()
        newest = anyio.Event()
        self._latest[key] = (term, newest)
        return newest

    async def _search_additions(
        self,
        term: str,
        wanted: dict[str, int],
        library: dict[str, Any],
        found: SearchResults,
    ) -> dict[str, list[dict[str, Any]]]:
        query = fold(term)
        library_artists = await self._library_entries()
        artists_index = {name: str(entry["id"]) for name, entry in library_artists.items()}
        additions: dict[str, list[dict[str, Any]]] = {}
        shown_albums: list[CatalogRelease] = []
        shown_songs: list[CatalogTrack] = []
        owned_artists = _entries(library, "artist")
        owned_albums = _entries(library, "album")
        owned_songs = _entries(library, "song")
        if (room := wanted.get("artist", 0) - len(owned_artists)) > 0:
            names = {fold(a.get("name")) for a in owned_artists}
            artists = _unique(a for a in found.artists if fold(a.name) not in names)
            artists.sort(key=lambda a: fold(a.name) != query)
            # An artist the library has is described by Navidrome.
            addresses = self.catalog is None or addresses_for_clients(self.catalog)
            entries = each(
                artists,
                lambda a: library_artists.get(fold(a.name)) or artist_entry(a, addresses=addresses),
                "artist",
            )
            ids = {a.get("id") for a in owned_artists}
            kept = []
            for entry in entries:  # two catalog artists may stand for one library artist
                if entry["id"] not in ids:
                    ids.add(entry["id"])
                    kept.append(entry)
            additions["artist"] = kept[:room]
        if (room := wanted.get("album", 0) - len(owned_albums)) > 0:
            known = [library_album(a) for a in owned_albums]
            # The albums of owned songs, unless listed (and typed) among the albums above.
            listed = {a.get("id") for a in owned_albums}
            known += [album_of_song(s) for s in owned_songs if s.get("albumId") not in listed]
            albums = await self._missing_releases(found.albums, known)
            albums.sort(key=lambda r: _album_rank(r, query))
            shown_albums = albums[:room]
        if (room := wanted.get("song", 0) - len(owned_songs)) > 0:
            songs = await self._missing_songs(found.songs, owned_songs)
            songs.sort(key=lambda t: query not in artist_names(t.artist))
            shown_songs = songs[:room]
        shown_albums, shown_songs = await self._with_artist_refs(
            found.artists, shown_albums, shown_songs
        )
        if "album" in wanted:
            additions["album"] = each(
                shown_albums, lambda r: album_entry(r, library=artists_index), "album"
            )
        if "song" in wanted:
            additions["song"] = each(
                shown_songs, lambda t: song_entry(t, library=artists_index), "song"
            )
        return {kind: entries for kind, entries in additions.items() if kind in wanted}

    async def _with_artist_refs(
        self,
        artists: tuple[CatalogArtist, ...],
        albums: list[CatalogRelease],
        songs: list[CatalogTrack],
    ) -> tuple[list[CatalogRelease], list[CatalogTrack]]:
        """Search results name no artist items: those of the entries shown are taken from the
        search's own artists by name (every name of the credit, split on "feat."), else
        asked for (within a short budget; without them, entries reference their artists
        through the song or album)."""
        by_name = {fold(a.name): a.ref for a in artists}

        def named(credit: str) -> tuple[CatalogRef, ...]:
            refs = [by_name.get(fold(p)) for p in featured_names(credit) or [credit]]
            return tuple(r for r in refs if r is not None) if all(refs) else ()

        albums = [a if a.artist_refs else replace(a, artist_refs=named(a.artist)) for a in albums]
        songs = [t if t.artist_refs else replace(t, artist_refs=named(t.artist)) for t in songs]
        missing_songs = tuple(t.ref.id for t in songs if not t.artist_refs)
        missing_albums = tuple(a.ref.id for a in albums if not a.artist_refs)
        if (not missing_songs and not missing_albums) or self.catalog is None:
            return albums, songs
        found: dict[str, tuple[CatalogRef, ...]] = {}
        try:
            with anyio.move_on_after(ARTIST_BUDGET):
                found = await self.catalog.artists_of(missing_songs, missing_albums)
        except CatalogError as exc:
            log.info("artists of search results unavailable: %s", exc.reason)
        albums = [replace(a, artist_refs=found.get(f"albums:{a.ref.id}", a.artist_refs))
                  for a in albums]  # fmt: skip
        songs = [replace(t, artist_refs=found.get(f"songs:{t.ref.id}", t.artist_refs))
                 for t in songs]  # fmt: skip
        return albums, songs

    def _expose(self, client: Client, albums: list[dict[str, Any]]) -> None:
        """Owned albums a search result or an artist page shows."""
        if self.exposed is not None:
            self.exposed(client, [str(a.get("id")) for a in albums if a.get("id")])

    async def _expose_search(
        self, call: RestCall, ctx: RequestContext, found: dict[str, Any]
    ) -> None:
        """The albums of a search's library results - albums, then the songs' albums -
        for a verified caller the search guard is not holding back."""
        if self.exposed is None:
            return
        caller = await ctx.caller()
        if caller is None:
            return
        client = (caller.username, call.client)
        if self.search_guard is not None and self.search_guard.bursting(client):
            return
        albums = _entries(found, "album")
        albums += [{"id": s.get("albumId")} for s in _entries(found, "song")]
        unique = {str(a["id"]): a for a in albums if a.get("id")}
        self._expose(client, list(unique.values()))

    async def _library(self) -> Mapping[str, str]:
        """The library's artists by folded name (none when unavailable)."""
        return {name: str(entry["id"]) for name, entry in (await self._library_entries()).items()}

    async def _library_entries(self) -> dict[str, dict[str, Any]]:
        if self.library_artists is None:
            return {}
        try:
            return await self.library_artists.entries()
        except Exception as exc:
            log.info("library artists unavailable: %s", type(exc).__name__)
            return {}

    async def _library_artist(self, name: str) -> str | None:
        """The library artist with exactly this (folded) name: from the index, else asked
        live (an artist added since the index was fetched)."""
        return (await self._library_artist_checked(name))[0]

    async def _library_artist_checked(self, name: str) -> tuple[str | None, bool]:
        """(The library artist with exactly this (folded) name, whether that is known): from
        the index of album artists, else asked live - Navidrome's search finds every artist
        with songs in the library, guests on a song too. Not known: Navidrome not asked."""
        wanted = fold(name)
        if not wanted:
            return None, True
        if found := (await self._library()).get(wanted):
            return found, True
        if self.navidrome is None:
            return None, False
        try:
            answer = await self.navidrome.subsonic(
                "search3",
                [("query", name), ("artistCount", "100"), ("albumCount", "0"), ("songCount", "0")],
            )
        except NavidromeError as exc:
            log.info("library artist lookup failed: %s", exc)
            return None, False
        artists = answer.get("searchResult3", {}).get("artist", [])
        found = next((str(a["id"]) for a in artists if fold(a.get("name")) == wanted), None)
        return found, True

    # --- artist pages ------------------------------------------------------------------------

    async def artist_page(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        """A library artist's page, with the catalog releases of the artist of that name
        that the library lacks."""
        catalog = self.catalog
        if catalog is None or call.fmt == "jsonp" or call.http_method == "HEAD":
            return None
        if not call.get("id"):
            return None
        caller = await ctx.caller()
        if caller is None:
            return None
        answer = await self._navidrome(call)
        if answer is None:
            return None

        def releases(owned: list[dict[str, Any]], name: str) -> Awaitable[Any]:
            return self._discography(catalog, name, owned)

        return await self._with_releases(
            answer, releases, "name:{}", (caller.username, call.client), accepts_gzip(call)
        )

    async def catalog_artist_page(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        """A catalog artist the library has (an artist of the same name): the library
        artist's page with this artist's missing releases, so no album shows twice. None:
        the virtual view answers."""
        catalog, text = self.catalog, call.get("id") or ""
        cid = CatalogId.parse(text)
        if cid is None or cid.kind != "ar" or catalog is None or self.navidrome is None:
            return None
        if cid.ref.catalog != catalog.key or call.fmt == "jsonp" or call.http_method == "HEAD":
            return None
        caller = await ctx.caller()
        if caller is None:
            return None  # the virtual view forwards: Navidrome's credential error
        deadline = self.clock() + self.budget  # one wait for the whole page
        artist, _ = await self._within_budget(f"artist:{cid}", lambda: catalog.artist(cid.ref.id))
        native = await self._library_artist(artist.name) if artist is not None else None
        if native is None:
            return None
        answer = await self._navidrome(
            call.rewritten(lambda k, v: native if k == "id" and v == text else None)
        )
        if answer is None:
            return None

        def releases(owned: list[dict[str, Any]], name: str) -> Awaitable[Any]:
            return catalog.artist_releases(cid.ref.id)

        return await self._with_releases(
            answer,
            releases,
            f"artist:{cid.ref}",
            (caller.username, call.client),
            accepts_gzip(call),
            budget=max(0.0, deadline - self.clock()),  # what the artist's lookup left
        )

    async def _with_releases(
        self,
        answer: LibraryAnswer,
        releases: Callable[[list[dict[str, Any]], str], Awaitable[Any]],
        key: str,
        client: Client,
        compress: bool,
        *,
        budget: float | None = None,
    ) -> HandlerResult:
        """``budget``: what is left of the request's one wait for the catalog (None: all
        of it)."""
        found = await answer.parsed("artist")
        if found is None:
            return answer.reply(compress=compress)
        document, artist = found
        owned = _entries(artist, "album")
        name = str(artist.get("name") or "")
        if fold(name) not in _NOBODY:
            self._expose(client, owned)
        catalog = self.catalog
        if not fold(name) or catalog is None:
            return answer.reply(compress=compress)
        # Saved per catalog and region: another one has other releases and IDs.
        scope = catalog_scope(catalog)
        discography = await self._discography_for(
            f"{scope}:{key.format(fold(name))}", lambda: releases(owned, name), client, budget
        )
        self._stands_for(name, discography)
        try:
            additions = await self._missing_releases(
                discography or (), [library_album(a) for a in owned]
            )
        except Exception as exc:  # Navidrome's answer, rather than asking it again
            log.warning("artist page additions failed: %s", type(exc).__name__)
            return answer.reply(compress=compress)
        if not additions:
            return answer.reply(compress=compress)
        library = await self._library()
        added = each(additions, lambda r: album_entry(r, library=library), "album")
        artist["album"] = [*owned, *added]
        syncing = self.artist_sync is not None and self.artist_sync.bursting(client)
        if self.prefetch is not None and not syncing:  # a sync looks at no page
            self.prefetch.page(client, added)
        return answer.reply(document=document, compress=compress)

    def _stands_for(self, name: str, releases: Iterable[CatalogRelease] | None) -> None:
        """The catalog artist a library artist's page showed releases of is the one its
        name stands for (getTopSongs names artists): the one most of them are credited to."""
        if self.shown is None or not releases:
            return
        wanted = fold(name)
        credited = Counter(
            r.artist_refs[0]
            for r in releases
            if len(r.artist_refs) == 1 and fold(r.artist) == wanted
        )
        if credited:
            self.shown.shown([CatalogArtist(credited.most_common(1)[0][0], name)])

    async def _discography_for(
        self,
        key: str,
        fetch: Callable[[], Awaitable[Iterable[CatalogRelease]]],
        client: Client,
        budget: float | None = None,
    ) -> tuple[CatalogRelease, ...] | None:
        """A saved discography at once (refreshed in the background when old); without
        one, the catalog's within the budget. A client opening many pages that would ask
        the catalog (no saved discography, or an old one) is syncing: those pages get
        the saved discography without a refresh, or none."""
        saved = await self.discographies.get(key) if self.discographies is not None else None
        if saved is not None and self.artwork is not None:
            self.artwork.releases(saved.releases)  # their covers need no album request
        found, _ = await self._saved_for(
            key,
            lambda: _as_tuple(fetch()),
            (saved.releases, saved.fresh) if saved is not None else None,
            self.discographies.save if self.discographies is not None else None,
            self._syncing(client, key, self.artist_sync, "artist pages", "discography"),
            "discography",
            budget,
        )
        return found

    async def _saved_for[T](
        self,
        key: str,
        fetch: Callable[[], Awaitable[tuple[T, ...]]],
        saved: tuple[tuple[T, ...], bool] | None,
        save: Callable[[str, tuple[T, ...]], Awaitable[None]] | None,
        syncing: Callable[[], bool],
        what: str,
        budget: float | None = None,
    ) -> tuple[tuple[T, ...] | None, str]:
        """(A saved list at once (refreshed in the background when old); without one, the
        catalog's within the budget; while ``syncing`` (asked only when the catalog
        would be), the saved list as it is, or none - and what happened, for the log)."""

        async def fetched() -> tuple[T, ...]:
            found = await fetch()
            if save is not None:
                try:
                    await save(key, found)
                except Exception as exc:  # the answer is still good for this page
                    log.warning("%s not saved: %s", what, type(exc).__name__)
            return found

        items, fresh = saved if saved is not None else (None, False)
        if fresh:
            return items, "saved"
        if syncing():
            return items, "skipped: sync" + (" (the saved list)" if items is not None else "")
        if items is not None:
            _, outcome = await self._within_budget(key, fetched, wait=False)
            return items, f"saved ({outcome})"
        return await self._within_budget(key, fetched, budget=budget)

    def _syncing(
        self, client: Client, key: str, sync: Bursts | None, pages: str, saved: str
    ) -> Callable[[], bool]:
        """Whether the client is walking many pages that would ask the catalog (this one
        counted when asked)."""

        def syncing() -> bool:
            if sync is None:
                return False
            burst = sync.note(client, key)
            if burst is Burst.STARTED:
                log.info(
                    "%s: %s opened %d pages without a fresh saved %s within %gs (a sync?);"
                    " answering those without asking the catalog",
                    pages,
                    client[1] or "a client",
                    sync.count(client),
                    saved,
                    sync.window,
                )
            return burst is not Burst.NO

        return syncing

    # --- top songs ----------------------------------------------------------------------------

    async def top_songs(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        """``getTopSongs``: the catalog's top songs of the artist, as an artist's page
        shows them - also for an artist the library has songs of: after Navidrome's
        own top songs, when it has any (as it gives them), the catalog's that those do
        not hold, in the catalog's order, up to the count. A song the library has (an
        owned one, a placeholder) is the library's own entry for the client; the others
        are catalog songs. Navidrome's answer alone whenever the catalog has no list in
        time, or the library's songs cannot be told. The artist is named, or given by an
        ID (Navidrome 0.64.2 takes one): a catalog artist's, or a library artist's own.
        None (before any work): the catalog IDs in the request are mapped as in any
        method."""
        catalog = self.catalog
        if catalog is None or call.fmt == "jsonp" or call.http_method == "HEAD":
            return None
        name = _SPACES.sub(" ", (call.get("artist") or "").strip())
        ident = call.get("id") or ""
        cid = CatalogId.parse(ident)
        if cid is not None and (cid.kind != "ar" or cid.ref.catalog != catalog.key):
            return None  # a catalog ID, but no artist's of this catalog: mapped as any
        if not ident and (not fold(name) or fold(name) in _NOBODY):
            return None
        count = _count(call.get("count"), TOP_COUNT)
        if count <= 0:
            return None
        caller = await ctx.caller()
        if caller is None:
            return None  # Navidrome answers with its own credential error
        deadline = self.clock() + self.budget  # one budget for the whole request, from here
        library_id = ident if ident and cid is None else None
        if library_id is not None:  # a library artist's own ID: the artist of that name
            name = ""
            with anyio.move_on_after(self.budget):  # (not found in time: Navidrome's answer)
                name = await self._library_name(library_id) or ""
            if not fold(name) or fold(name) in _NOBODY:
                return None  # not an artist Navidrome names: its own answer
        report = TopSongsReport(call, by_id=bool(ident))
        try:
            client = (caller.username, call.client)
            return await self._top_songs(
                call, catalog, cid, name, count, client, report, library_id, deadline
            )
        finally:
            log.info("%s", report)

    async def _library_name(self, artist_id: str) -> str | None:
        """The name of the library artist with this ID: from the index of album artists,
        else asked of Navidrome (a guest on a song) - once for requests at the same time,
        and remembered a while."""
        for entry in (await self._library_entries()).values():
            if str(entry.get("id")) == artist_id:
                return str(entry.get("name") or "") or None
        if self.navidrome is None:
            return None
        async with self._naming.hold(artist_id):
            known = self._names.get(artist_id)
            if known is not None and known[1] > self.clock():
                return known[0]  # (another request asked meanwhile)
            try:
                answer = await self.navidrome.subsonic("getArtist", [("id", artist_id)])
            except NavidromeError:
                return None
            name = str((answer.get("artist") or {}).get("name") or "") or None
            if name is not None:
                if len(self._names) >= NAMES_REMEMBERED:
                    self._names.clear()
                self._names[artist_id] = (name, self.clock() + NAME_SECONDS)
            return name

    async def _top_songs(
        self,
        call: RestCall,
        catalog: Catalog,
        cid: CatalogId | None,
        name: str,
        count: int,
        client: Client,
        report: TopSongsReport,
        library_id: str | None = None,
        deadline: float | None = None,
    ) -> HandlerResult:
        """None: what Navidrome answers, the request's catalog IDs mapped as in any
        method. ``library_id``: the library artist the request gave by its own ID.
        ``deadline``: when the request's one budget ends (begun before that artist's name
        was looked up)."""
        if deadline is None:
            deadline = self.clock() + self.budget  # one budget for the whole request

        def left() -> float:
            return max(0.0, deadline - self.clock())

        scope = catalog_scope(catalog)
        store = self.discographies
        burst = f"top:{cid.ref if cid is not None else fold(name)}"
        sync = self._syncing(client, burst, self.top_sync, "top songs", "top song list")
        artist: CatalogRef | None
        if cid is not None:  # the artist's name, to tell whether the library has it
            artist = cid.ref
            known = self.shown.name_of(artist) if self.shown is not None else None
            if known is None and store is not None:  # saved with a list before (a restart)
                known = await store.get_name(f"{scope}:top:who:{artist}")
            if known is None:
                if self._quiet(client) or sync():
                    report.catalog = "skipped: sync or burst"
                    return None  # the ID mapped as in any method
                ref = cid.ref
                looked_up, report.catalog = await self._within_budget(
                    f"artist:{cid}", lambda: catalog.artist(ref.id), budget=left()
                )
                if looked_up is None:  # (the lookup the mapping would need failed too)
                    return Forward(call)
                known = looked_up.name
            if fold(known) in _NOBODY:
                return None
            name = known
        else:
            artist = self.shown.find(name) if self.shown is not None else None
        native, sure = library_id, library_id is not None
        if library_id is None:
            with anyio.move_on_after(left()):  # the library's own answer: quick, but bounded
                native, sure = await self._library_artist_checked(name)
        report.library = "no" if sure and native is None else "yes" if sure else "unknown"
        if not sure:
            return None  # Navidrome's own answer (a catalog ID mapped as in any method)
        # What Navidrome itself answers, for an artist the library has: its top songs stay,
        # as it gives them - the catalog's follow.
        asked = call
        if cid is not None and native is not None:
            asked = call.rewritten(lambda k, v: native if k == "id" else None)
        own: LibraryAnswer | None = None
        document: dict[str, Any] | None = None
        listed: dict[str, Any] = {}
        mine: list[dict[str, Any]] = []
        if native is not None:
            with anyio.move_on_after(left()):
                own = await self._navidrome(asked)
            if own is None:  # unreachable, or not within the budget: the forward is its answer
                return Forward(asked)
            found = await own.parsed("topSongs")
            if found is None:
                return own.reply(compress=accepts_gzip(call))  # an error: as it gave it
            document, listed = found
            mine = _entries(listed, "song")
            report.own = len(mine)
            if len(mine) >= count:
                return own.reply(compress=accepts_gzip(call))

        def navidrome_s() -> HandlerResult:
            """Navidrome's own answer, unchanged."""
            if own is not None:
                return own.reply(compress=accepts_gzip(call))
            return Forward(call)

        term = _term(name)
        resolved: list[CatalogRef] = []  # the artist the fetch found, for its key
        resolved_name: list[str] = []
        name_key = f"{scope}:top:name:{fold(name)}"

        async def fetch() -> tuple[CatalogTrack, ...]:
            ref, credit = artist, name
            if ref is None:  # a name not shown: the first exact match of a catalog search
                results = await catalog.search(term, SEARCH_LIMIT)
                exact = [a for a in results.artists if fold(a.name) == fold(name)]
                if not exact:
                    return ()
                ref, credit = exact[0].ref, exact[0].name
            resolved.append(ref)
            resolved_name.append(credit)
            try:
                songs = await catalog.top_songs(ref.id, TOP_SONGS)
            except CatalogError as exc:
                if exc.kind != "not_found":
                    raise
                return ()  # the catalog has no list for this artist: saved as none
            # The view names no artist items: the artist's own for its songs alone.
            return tuple(
                replace(t, artist_refs=(ref,))
                if not t.artist_refs and fold(t.artist) == fold(credit)
                else t
                for t in songs
            )

        async def save(key: str, songs: tuple[CatalogTrack, ...]) -> None:
            """Saved under the key asked for, the name (a name request: found after a
            restart) and the artist, with the artist's name (an ID request after a
            restart)."""
            assert store is not None
            keys = {key, f"{scope}:top:artist:{resolved[0]}"} if resolved else {key}
            if cid is None:
                keys.add(name_key)
            for each_key in sorted(keys):
                await store.save_songs(each_key, songs)
            if resolved:
                await store.save_name(f"{scope}:top:who:{resolved[0]}", resolved_name[0])

        what = f"artist:{artist}" if artist is not None else f"name:{fold(name)}"
        key = f"{scope}:top:{what}"
        saved = await store.get_songs(key) if store is not None else None
        if saved is not None:  # their covers need no album request, a play no song lookup
            if self.artwork is not None:
                self.artwork.tracks(saved.songs)
            remember = getattr(catalog, "remember", None)
            if remember is not None:
                remember(saved.songs)
        quiet = self._quiet(client)
        songs, report.catalog = await self._saved_for(
            key,
            fetch,
            (saved.songs, saved.fresh) if saved is not None else None,
            save if store is not None else None,
            lambda: quiet or sync(),
            "top song list",
            budget=left(),
        )
        if (  # a list saved under the artist: the name's too, for after a restart
            cid is None
            and key != name_key
            and songs
            and store is not None
            and await store.get_songs(name_key) is None
        ):
            await store.save_songs(name_key, songs)
        if not songs:
            return navidrome_s()
        # Each of the catalog's songs as the library has it (its own entry for the
        # client), else as a catalog song; none twice.
        held = {str(entry.get("id")) for entry in mine}
        recordings = {isrc for entry in mine for isrc in _isrcs(entry)}
        added: list[dict[str, Any]] = []
        natives = 0
        chosen: list[tuple[CatalogTrack, str | None]] = []
        try:
            with anyio.fail_after(left()):  # within the request's one budget
                waiting = merge_twins(songs, self.twins)  # clean/explicit twins
                # Only as many songs as the answer still takes are matched with the library.
                while waiting and len(mine) + len(chosen) < count:
                    room = count - len(mine) - len(chosen)
                    batch, waiting = waiting[:room], waiting[room:]
                    songs_of = await self.library_songs.native(batch, search=native is not None)
                    # (A placeholder that plays an owned song is that song once more.)
                    backing = await self.library_songs.backing(songs_of.values())
                    for track in batch:
                        song = songs_of.get(track.ref)
                        same = {s for s in (song, backing.get(song or "")) if s}
                        isrc = track.isrc.upper() if track.isrc else None
                        if (
                            same & held
                            or (isrc is not None and isrc in recordings)
                            or any(_same_recording(track, entry) for entry in mine)
                        ):
                            continue  # in Navidrome's own list, or the same recording again
                        held |= same
                        if isrc is not None:
                            recordings.add(isrc)
                        chosen.append((track, song))
                wanted = [song for _, song in chosen if song is not None]
                entries = await self.library_songs.entries(call, wanted) if wanted else {}
                library = await self._library()
        except (TimeoutError, Unknown) as exc:
            log.info("top songs: the library's songs not told (%s)", type(exc).__name__)
            report.catalog += "; the library's songs not told"
            return navidrome_s()
        for track, song in chosen:
            if song is None:
                added += each([track], lambda t: song_entry(t, library=library), "song")
            elif song in entries:  # (else: a song Navidrome does not give this caller)
                natives += 1
                added.append(entries[song])
        report.songs, report.native = len(added), natives
        if not added:
            return navidrome_s()
        if own is not None and document is not None:
            listed["song"] = [*mine, *added]
            return own.reply(document=document, compress=accepts_gzip(call))
        return subsonic_ok(call, {"topSongs": {"song": added}}, self.server())

    def _quiet(self, client: Client) -> bool:
        """The client is walking artist pages or searching in a burst: no catalog work."""
        guards = (self.artist_sync, self.top_sync, self.search_guard)
        return any(g is not None and g.bursting(client) for g in guards)

    async def _discography(
        self, catalog: Catalog, name: str, owned: list[dict[str, Any]]
    ) -> tuple[CatalogRelease, ...]:
        """The releases of the catalog artist with this name. Several artists of that name:
        of the first three, the one whose releases share the most titles with the library
        (none shared: none)."""
        if not _term(name) or fold(name) in _NOBODY:
            return ()
        wanted = fold(name)
        artists = [
            a
            for a in (await catalog.search(_term(name), SEARCH_LIMIT)).artists
            if fold(a.name) == wanted
        ]
        artists = _unique(artists)
        if len(artists) == 1:
            return await catalog.artist_releases(artists[0].ref.id)
        titles = {title_key(a.get("name")) for a in owned}
        best: tuple[CatalogRelease, ...] = ()
        shared = 0
        for candidate in artists[:3]:
            releases = await catalog.artist_releases(candidate.ref.id)
            count = sum(1 for r in releases if title_key(r.title) in titles)
            if count > shared:
                best, shared = releases, count
        return best

    async def _within_budget(
        self,
        key: str,
        work: Callable[[], Awaitable[T]],
        *,
        wait: bool = True,
        stop: anyio.Event | None = None,
        songs_only: bool = False,
        budget: float | None = None,
    ) -> tuple[T | None, str]:
        """``work``'s result if it is ready within the budget, and what happened: cached,
        asked, joined (a running lookup), timed out, failed, superseded (``stop`` was set)
        or skipped. Requests for the same ``key`` share one lookup. With a background task
        group (``spawn``) the lookup goes on after the budget, so the catalog's cache has
        the answer for the next request; at most a few run at a time (a client walking
        every artist page must not queue up catalog work). After the catalog failed,
        additions rest for a while. ``wait=False`` only starts the lookup (a refresh).
        Song-only searches (``songs_only``) have one lookup of their own at a time, and
        their failures do not rest the others. ``budget``: less than the budget (what
        is left of a request's)."""
        wait_for = self.budget if budget is None else min(budget, self.budget)
        if self.clock() < self.resting_until:
            return None, "skipped: resting after a failure"
        running = self._running.get(key)
        joined = running is not None
        cached = False
        if running is None:
            lane = [k for k in self._running if (k in self._song_lookups) == songs_only]
            if len(lane) >= (SONG_LOOKUPS if songs_only else MAX_PENDING):
                if not songs_only:  # a client's track lookups: dozens at once, not worth a line
                    log.info("catalog lookup skipped: %d still running", len(lane))
                return None, "skipped: busy"
            if not wait and self.spawn is None:
                return None, "not refreshed"
            answered = self._answered.get(key)
            cached = answered is not None and self.clock() - answered < self.cache_seconds
            running = self._running[key] = (anyio.Event(), [])
            if songs_only:
                self._song_lookups.add(key)
            done, box = running

            async def run() -> None:
                try:
                    box.append(await work())
                    self._succeeded(key)
                except CatalogError as exc:
                    log.info("catalog lookup unavailable: %s", exc.reason)
                    if exc.kind in ("unavailable", "rate_limited") and not songs_only:
                        self.resting_until = self.clock() + self.rest_seconds
                except Exception as exc:
                    log.warning("catalog lookup failed: %s", type(exc).__name__)
                finally:
                    self._running.pop(key, None)
                    self._song_lookups.discard(key)
                    done.set()

            if self.spawn is None:
                with anyio.move_on_after(wait_for):
                    await run()
                if box:
                    return box[0], "cached" if cached else "asked"
                return None, "failed" if done.is_set() else "timed out"
            self.spawn(run)
        if not wait:
            return None, "refreshing"
        done, box = running
        with anyio.move_on_after(wait_for):
            await _first(done, stop)
        if box:
            return box[0], "joined" if joined else "cached" if cached else "asked"
        if done.is_set():
            return None, "failed"
        if stop is not None and stop.is_set():
            return None, "superseded"
        return None, "timed out"

    def _succeeded(self, key: str) -> None:
        if len(self._answered) > 8192:
            horizon = self.clock() - self.cache_seconds
            self._answered = {k: at for k, at in self._answered.items() if at > horizon}
        self._answered[key] = self.clock()

    # --- de-duplication against the library -------------------------------------------------

    async def _missing_releases(
        self, releases: Iterable[CatalogRelease], library: list[LibraryAlbum]
    ) -> list[CatalogRelease]:
        """One card per album the library does not have (edition rules)."""
        candidates = list(releases)
        refs = [str(r.ref) for r in candidates]
        committed = await self._known("SELECT ref AS k FROM releases", "ref", refs)
        known = [*library, *(as_library(r) for r in candidates if str(r.ref) in committed)]
        return dedupe_editions(
            [r for r in candidates if str(r.ref) not in committed], known, twins=self.twins
        )

    async def _missing_songs(
        self, tracks: Iterable[CatalogTrack], owned: list[dict[str, Any]]
    ) -> list[CatalogTrack]:
        candidates = list(tracks)
        linked = await self._known(
            "SELECT track_ref AS k FROM track_links", "track_ref", [str(t.ref) for t in candidates]
        )
        isrcs = {i for s in owned for i in _isrcs(s)}
        kept: list[CatalogTrack] = []
        for track in candidates:
            if str(track.ref) in linked or (track.isrc and track.isrc in isrcs):
                continue
            if any(_same_song(track, song) for song in owned):
                continue
            if track.isrc:
                isrcs.add(track.isrc)  # one version of a recording
            if all(t.ref != track.ref for t in kept):
                kept.append(track)
        return merge_twins(kept, self.twins)  # clean/explicit twins

    async def _known(self, query: str, column: str, values: list[str]) -> set[str]:
        found: set[str] = set()
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            if not chunk:
                continue
            marks = ",".join("?" * len(chunk))
            rows = await self.store.fetchall(f"{query} WHERE {column} IN ({marks})", chunk)
            found |= {str(row["k"]) for row in rows}
        return found

    # --- Navidrome ---------------------------------------------------------------------------

    def _this_library(self, call: RestCall) -> bool:
        folders = call.getall("musicFolderId")
        return not folders or str(self.library_id) in folders

    async def _navidrome(self, call: RestCall) -> LibraryAnswer | None:
        """The client's request as Navidrome answers it, uncompressed and read in full."""
        return await library_answer(self.upstream, call)


# --- helpers ------------------------------------------------------------------------------


def _printable(text: str) -> str:
    """A client-given value fit for one log line."""
    return "".join(ch if ch.isprintable() else "?" for ch in text[:64])


_GO_INT = re.compile(r"[+-]?[0-9]+")  # what Go's strconv.ParseInt(text, 10, 64) reads


def _count(value: str | None, default: int) -> int:
    """A requested count as Navidrome reads it (Go's ``ParseInt``, base 10, 64 bits): a
    sign and decimal digits alone - no spaces, no underscores - within a signed 64-bit
    integer; none or unreadable, the default."""
    if not value or _GO_INT.fullmatch(value) is None:
        return default
    digits = value.lstrip("+-").lstrip("0") or "0"
    if len(digits) > 19:  # past 64 bits (and no number Python would refuse to read)
        return default
    number = -int(digits) if value.startswith("-") else int(digits)
    return number if -(2**63) <= number < 2**63 else default


async def _as_tuple[T](found: Awaitable[Iterable[T]]) -> tuple[T, ...]:
    return tuple(await found)


def _number(value: str | None, default: int) -> str:
    if value is None or value == "":
        return str(default)
    try:
        return str(int(value))
    except ValueError:
        return "invalid"


def _term(query: str | None) -> str:
    """The search term: some clients send an empty query as two quotes."""
    text = _SPACES.sub(" ", (query or "").strip())
    return "" if text in ('""', "''") else text.strip('"').strip().lower()


def _guard_name(songs_only: bool) -> str:
    return "song search guard" if songs_only else "search guard"


def _wanted(call: RestCall) -> dict[str, int]:
    """Requested counts of the types whose first page is asked for; empty for a sync-sized
    request or unreadable numbers (Navidrome answers those as it does)."""
    wanted: dict[str, int] = {}
    for kind, count_key, offset_key in _TYPES:
        try:
            count = int(call.get(count_key) or 20)
            offset = int(call.get(offset_key) or 0)
        except ValueError:
            return {}
        if count > MAX_COUNT:
            return {}
        if offset == 0 and count > 0:
            wanted[kind] = count
    return wanted


def _room(found: dict[str, Any], wanted: dict[str, int]) -> bool:
    """Whether Navidrome's search answer leaves room for additions."""
    return any(count > len(_entries(found, kind)) for kind, count in wanted.items())


def _entries(container: dict[str, Any], key: str) -> list[dict[str, Any]]:
    found = container.get(key)
    return [e for e in found if isinstance(e, dict)] if isinstance(found, list) else []


def _unique(artists: Iterable[CatalogArtist]) -> list[CatalogArtist]:
    seen: set[str] = set()
    kept = []
    for artist in artists:
        if str(artist.ref) not in seen:
            seen.add(str(artist.ref))
            kept.append(artist)
    return kept


def _album_rank(release: CatalogRelease, query: str) -> tuple[bool, bool]:
    """The searched artist's releases first; other artists' albums before their singles."""
    exact = query in artist_names(release.artist)
    return not exact, not exact and release.kind in (ReleaseKind.SINGLE, ReleaseKind.EP)


def _kind(entry: dict[str, Any]) -> ReleaseKind | None:
    types = entry.get("releaseTypes")
    for value in types if isinstance(types, list) else []:
        try:
            return ReleaseKind(str(value).lower())
        except ValueError:
            continue
    return ReleaseKind.COMPILATION if entry.get("isCompilation") is True else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _clean(entry: dict[str, Any]) -> bool:
    """A clean edition or version, as Navidrome shows it (the album's version, or the
    advisory tag)."""
    return entry.get("explicitStatus") == "clean" or str(entry.get("version")).lower() == "clean"


def library_album(entry: dict[str, Any]) -> LibraryAlbum:
    """A Navidrome album entry, for edition de-duplication."""
    return LibraryAlbum(
        title=str(entry.get("name") or ""),
        artist=str(entry.get("artist") or entry.get("displayArtist") or ""),
        year=_int(entry.get("year")),
        tracks=_int(entry.get("songCount")),
        kind=_kind(entry),
        clean=_clean(entry),
    )


def album_of_song(entry: dict[str, Any]) -> LibraryAlbum:
    """The album of a Navidrome song entry (its size unknown)."""
    artist = entry.get("displayAlbumArtist") or entry.get("artist") or ""
    return LibraryAlbum(
        str(entry.get("album") or ""), str(artist), _int(entry.get("year")), clean=_clean(entry)
    )


def as_library(release: CatalogRelease) -> LibraryAlbum:
    """A catalog release that is in the library (committed or filled)."""
    tracks = release.track_count or len(release.tracks) or None
    return LibraryAlbum(
        release.title, release.artist, release.year, tracks, release.kind, release.clean
    )


async def _first(done: anyio.Event, stop: anyio.Event | None) -> None:
    """Until ``done`` is set, or ``stop`` is."""
    if stop is None:
        await done.wait()
        return
    async with anyio.create_task_group() as tg:

        async def wait(event: anyio.Event) -> None:
            await event.wait()
            tg.cancel_scope.cancel()

        tg.start_soon(wait, done)
        tg.start_soon(wait, stop)


def _unbroken(view: Callable[[RestCall, RequestContext], Awaitable[HandlerResult]]) -> Handler:
    """A failure in the additions must not break the client's search or artist page:
    Navidrome's own answer is used instead."""

    async def handle(call: RestCall, ctx: RequestContext) -> HandlerResult:
        try:
            return await view(call, ctx)
        except Exception as exc:
            log.warning("%s additions failed: %s", call.name, type(exc).__name__)
            return None

    return handle
