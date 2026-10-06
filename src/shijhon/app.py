"""Application assembly: settings in, one ASGI app out."""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import anyio
import anyio.abc
import anyio.to_thread

from shijhon.catalog.artwork import ArtworkCache, ArtworkIndex
from shijhon.catalog.base import Catalog
from shijhon.catalog.base import scope as catalog_scope
from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.model import Twins
from shijhon.catalog.paced import PacedCatalog
from shijhon.catalog.setup import build_catalog
from shijhon.cleanup import Cleanup, OldIds
from shijhon.config import Settings
from shijhon.dashboard.app import PATH as DASHBOARD_PATH
from shijhon.dashboard.app import Dashboard, printable
from shijhon.dashboard.live import addon_limits, ahead_wait
from shijhon.dashboard.saved import (
    Locked,
    SavedSettings,
    effective,
    hidden_by_environment,
    locked_keys,
    out_of_place,
    withheld,
)
from shijhon.dashboard.sections import fields_of
from shijhon.delivery.ahead import AheadGate
from shijhon.delivery.dash import Dash, ffmpeg_path
from shijhon.delivery.download_first import DownloadFirst
from shijhon.delivery.expiry import DeliveredAudio
from shijhon.delivery.intercept import Interceptor
from shijhon.delivery.limits import UserLimits
from shijhon.delivery.listening import Listening
from shijhon.delivery.netpolicy import Resolver, system_resolver
from shijhon.delivery.playback import Deliverer, PlaybackSettings
from shijhon.delivery.sources import SourceRegistry
from shijhon.delivery.warm import WarmAhead
from shijhon.fill.fills import FillPolicy, Fills
from shijhon.fill.library_pass import LibraryPass
from shijhon.fill.refresh import Refresh
from shijhon.matching.matcher import Matcher
from shijhon.navidrome.checks import PlaceholderWrites, StartupChecks
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.navidrome.usage import configured
from shijhon.placeholders.backing import OwnedRecordings
from shijhon.placeholders.engine import PlaceholderEngine
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.silence import SilenceMaker
from shijhon.proxy.app import Handler, ProxyApp
from shijhon.proxy.auth import CredentialChecker
from shijhon.proxy.forwarding import Forwarding, learn_user_header
from shijhon.proxy.upstream import Receive, Scope, Send, Upstream
from shijhon.store import Store
from shijhon.store.writer import WriterLock
from shijhon.views.additions import CatalogAdditions
from shijhon.views.bursts import Bursts
from shijhon.views.catalog_ids import CatalogIds
from shijhon.views.commits import COMMITS, Commits
from shijhon.views.complete import LISTS, CompleteAlbums
from shijhon.views.covers import CoverPrefetch, CoverSizes
from shijhon.views.discographies import Discographies
from shijhon.views.library_artists import LibraryArtists
from shijhon.views.removed import RemovedViews
from shijhon.views.reports import REPORTS, Reports
from shijhon.views.shown import ShownArtists
from shijhon.views.upcoming import UpcomingSongs
from shijhon.views.virtual import VirtualViews

log = logging.getLogger(__name__)
CLOSE_SECONDS = 10.0  # the longest the shutdown waits for one thing it closes
RESTART_AFTER = 0.5  # seconds after "Restart Shijhon": its answer reaches the browser first
RESTART_WRITE_WAIT = 30.0  # ... and the longest it waits for a library write under way
REPORT_SECONDS = 60.0  # a failing kind of background work is an error in the log this often


@dataclass
class Services:
    """Everything that needs the database or Navidrome; built at startup."""

    store: Store
    navidrome: NavidromeService
    scans: ScanCoordinator
    engine: PlaceholderEngine
    sources: SourceRegistry
    deliverer: Deliverer
    download_first: DownloadFirst
    interceptor: Interceptor
    catalog: Catalog | None = None
    views: VirtualViews | None = None
    commits: Commits | None = None
    additions: CatalogAdditions | None = None
    fills: Fills | None = None
    library_pass: LibraryPass | None = None
    refresh: Refresh | None = None  # "refresh from the current catalog"
    cleanup: Cleanup | None = None  # unused releases taken out again
    old_ids: OldIds | None = None  # ... and added back by a use of their old IDs
    removed: RemovedViews | None = None  # ... whose views write nothing


class ShijhonApp:
    """The ASGI application: lifespan management around the proxy."""

    def __init__(
        self,
        settings: Settings,
        handlers: dict[str, Handler] | None = None,
        *,
        resolver: Resolver = system_resolver,
        catalog: Catalog | None = None,
    ) -> None:
        self.configured = settings  # the configuration file, the environment, the defaults
        # The dashboard's settings the environment sets: they win over saved values.
        self.locked: Locked = locked_keys(settings)
        self.settings = settings  # at startup: with the values saved in the dashboard
        self.resolver = resolver
        self._catalog = catalog  # given by tests; otherwise built from the settings
        self.upstream = Upstream(settings.navidrome.url, timeout=settings.navidrome.timeout_seconds)
        self.checker = CredentialChecker(
            self.upstream,
            ttl=settings.server.credential_cache_seconds,
            proxy_user_header=settings.server.reverse_proxy_user_header,
            timeout=settings.navidrome.timeout_seconds,
        )
        # Who the client is, for Navidrome and the dashboard's sign-in limit.
        self.forwarding = Forwarding(
            settings.server.trusted_proxies,
            settings.server.reverse_proxy_user_header,
            user_auth=settings.server.reverse_proxy_auth,
        )
        self.proxy = ProxyApp(
            self.upstream,
            self.checker,
            max_parsed_body=settings.server.max_parsed_body_bytes,
            handlers=handlers,
            forwarding=self.forwarding,
        )
        self.services: Services | None = None
        self.dashboard = Dashboard(self, resolver=resolver)
        if settings.server.dashboard:
            self.proxy.mounts[DASHBOARD_PATH] = self.dashboard
        self.loop: asyncio.AbstractEventLoop | None = None
        self._background: anyio.abc.TaskGroup | None = None
        # What startup opened, closed by shutdown in the reverse order (also after a startup
        # that failed half way).
        self._opened: list[Callable[[], Awaitable[None]]] = []
        self._reported: dict[str, float] = {}  # background work -> its last error line
        # The dashboard's "Restart Shijhon": what stops the server so that the process exits
        # and is started again by whatever runs it (set by ``shijhon serve`` where something
        # does; None: nothing would start it again, and the dashboard does not offer it).
        self.restarter: Callable[[], object] | None = None  # (False: not begun)
        self.restarting = False  # asked for, and under way
        self.stopping: Callable[[], bool] | None = None  # the server was told to stop
        self._lock: Callable[[], Awaitable[None]] | None = None  # the writer lock's close
        # Why the next start would not bring Shijhon back (the configuration as it is now;
        # also set by ``shijhon serve``): asked before a restart stops anything.
        self.restart_check: Callable[[], str | None] | None = None

    async def startup(self) -> None:
        self.loop = asyncio.get_running_loop()  # lets tests and tools schedule work on it
        # One process writes the library: a second service on this state, or
        # `shijhon fills-undo --apply` while this one runs, is refused. Before anything
        # else is opened - and without the lock, nothing is: a state folder that cannot
        # hold one fails the start (AlreadyRunning and OSError alike).
        writer = WriterLock(self.configured.database_path)
        try:
            writer.acquire()
        except OSError as exc:
            raise RuntimeError(
                f"the state folder cannot hold Shijhon's lock ({writer.path}:"
                f" {exc.strerror or type(exc).__name__}): not started - nothing would keep"
                " a second Shijhon process from writing the library beside this one"
            ) from None
        self._opened.append(writer.aclose)
        self._lock = writer.aclose
        self.dashboard.listen()
        store = await Store.open(self.configured.database_path)
        self._opened.append(store.close)
        # Values saved in the dashboard win over the file; the environment wins over
        # them.
        saved = SavedSettings(store)
        values = await saved.all()
        settings, problems = effective(self.configured, values, locked=self.locked, startup=True)
        for name in hidden_by_environment(values, self.locked, self.configured):
            log.info("dashboard: the saved %s is not in use: the environment sets it", name)
        for section, reason in problems.items():
            log.warning(
                "dashboard: the saved %s settings are not in use (the configuration's are): %s",
                section,
                reason,
            )
        labels = {f.key: f.meta.label.lower() for f in fields_of("catalog", settings.catalog.kind)}
        for key, address in withheld(self.configured.catalog, settings.catalog):
            log.warning(
                "catalog: not sent: the configuration's %s (the %s saved in the dashboard"
                " is on another host)",
                labels[key],
                labels[address],
            )
        for key, address in out_of_place(self.configured, values, self.locked):
            log.warning(
                "catalog: not in use: the %s saved in the dashboard (it was saved for another"
                " %s; enter it again on the Catalog page if this one needs it - or, when the"
                " catalog cannot be connected without it, set it in the configuration)",
                labels.get(key, key),
                labels.get(address, address),
            )
        self.settings = settings
        navidrome = NavidromeService(
            settings.navidrome.url,
            settings.navidrome.user,
            settings.navidrome.service_password(),
            client_name=settings.navidrome.client_name,
            library_id=settings.navidrome.library_id,
            timeout=settings.navidrome.timeout_seconds,
        )
        self._opened.append(navidrome.aclose)
        if settings.navidrome.library_path is None:
            raise RuntimeError("navidrome.library_path must point at the library root")
        scans = ScanCoordinator(navidrome)
        # Recordings the owner has on other albums back placeholders and catalog plays.
        recordings = OwnedRecordings(
            navidrome,
            store,
            library_id=settings.navidrome.library_id,
            placeholder_folder=settings.placeholders.folder,
        )
        # Placeholders are written only while Navidrome keeps missing files.
        writes = PlaceholderWrites(navidrome, stated=settings.navidrome.purge_missing)
        engine = PlaceholderEngine(
            layout=Layout(settings.navidrome.library_path, settings.placeholders.folder),
            navidrome=navidrome,
            scans=scans,
            silence=SilenceMaker(),
            store=store,
            backing=recordings,
            writes_refused=writes.refused,
        )
        await engine.load_removed()  # requests with old IDs of removed releases
        await engine.load_pending()  # placeholders a stop left half written
        checks = StartupChecks(navidrome, engine, store, writes)
        self.spawn(checks.run)
        self.spawn(checks.keep_repairing)
        self.forwarding.learning()  # requests wait for it (briefly)
        if not self.try_spawn(lambda: learn_user_header(self.forwarding, navidrome)):
            self.forwarding.learned()
        delivery = settings.delivery
        sources = SourceRegistry(
            store,
            resolver=self.resolver,
            request_timeout=delivery.request_timeout_seconds,
            retire_after=delivery.pin_ttl_seconds,
            limits=addon_limits(delivery),  # what one add-on is sent in all
            cooldown_seconds=delivery.cooldown_seconds,
        )
        self._opened.append(sources.aclose)
        if settings.addons is not None:
            # The environment's list always applies; the file's until the dashboard
            # changed the list.
            if "addons" in settings.from_environment or await saved.addons_owner() is None:
                await sources.sync(settings.addons)
                log.info("add-ons from the configuration: %d", len(settings.addons))
            elif differs := await sources.differs(settings.addons):
                # The owner may have edited the file since: say so where it is seen.
                log.warning(
                    "add-ons: the configuration file's [[addons]] are not applied: the list was"
                    " changed in the dashboard, which keeps its own (the file differs in: %s);"
                    " the dashboard's Add-ons page hands the list back to the file",
                    ", ".join(printable(name) for name in differs),
                )
            else:
                log.info(
                    "add-ons: the list was changed in the dashboard, so the configuration"
                    " file's list is not applied (it is the same now)"
                )
        deliverer = Deliverer(
            sources,
            PlaybackSettings(
                budget_seconds=delivery.budget_seconds,
                max_attempts=delivery.max_attempts,
                pin_ttl_seconds=delivery.pin_ttl_seconds,
                cooldown_seconds=delivery.cooldown_seconds,
                seek_timeout_seconds=delivery.seek_timeout_seconds,
                routing=delivery.routing,
                reliable_source=delivery.reliable_source,
                primary_source=delivery.primary_source,
                primary_budget_seconds=delivery.primary_budget_seconds,
                primary_cooldown_timeouts=delivery.primary_cooldown_timeouts,
                primary_cooldown_switches=delivery.primary_cooldown_switches,
                cooldown_errors=delivery.cooldown_errors,
                max_wait_seconds=delivery.max_wait_seconds,
                availability_timeout_seconds=delivery.availability_timeout_seconds,
                reliable_lookup_after_seconds=delivery.reliable_lookup_after_seconds,
                primary_miss_hours=delivery.primary_miss_hours,
                primary_release_miss_minutes=delivery.primary_release_miss_minutes,
                length_tolerance_seconds=delivery.length_tolerance_seconds,
                length_tolerance_percent=delivery.length_tolerance_percent,
                retry_skip_seconds=delivery.retry_skip_seconds,
                prepare_when_not_ready=delivery.prepare_when_not_ready,
                warm_ahead_depth=delivery.warm_ahead_depth,
                warm_ahead_budget_seconds=delivery.warm_ahead_budget_seconds,
                dash_quality_from=delivery.dash_quality_from,
                dash_quality_to=delivery.dash_quality_to,
                dash_start=delivery.dash_start,
                dash_segments_at_once=delivery.dash_segments_at_once,
            ),
            spawn=self.spawn,
        )
        # DASH links: joined into files (in a folder of their own; what a stop left there
        # goes, before any request).
        dash = Dash(
            settings.state_dir / "dash",
            "",
            max_bytes=delivery.dash_cache_mb * 2**20,
            spawn=self.try_spawn,  # (not while stopping: then the join fails at once)
            clock=deliverer.clock,  # (the deadlines it is given are on the deliverer's)
        )
        dash.joins_at_once = delivery.dash_joins_at_once
        if await anyio.to_thread.run_sync(dash.clear):
            log.info("startup: joined DASH audio from before removed")
        if not delivery.dash:
            deliverer.dash_off = "turned off in the settings"
        elif (ffmpeg := ffmpeg_path()) is None:
            deliverer.dash_off = "ffmpeg was not found"
            log.warning(
                "DASH links of add-ons are not played: ffmpeg was not found on the PATH"
                " (direct links are not affected)"
            )
        else:
            dash.ffmpeg = ffmpeg
            deliverer.dash = dash
        # The fallbacks' order by measurement, kept across restarts.
        await sources.load_attempts(deliverer.clock)
        self.spawn(sources.keep_attempts)
        expiry = DeliveredAudio(
            store,
            engine,
            max_days=delivery.delivered_days,
            max_bytes=int(delivery.delivered_gb * 2**30),
            spawn=self.spawn,
        )
        limits = UserLimits(
            routings=delivery.user_routings,
            downloads=delivery.user_downloads,
            downloads_per_hour=delivery.user_downloads_per_hour,
            downloads_burst=delivery.user_download_burst,
        )
        download_first = DownloadFirst(
            deliverer,
            engine,
            store,
            settings.state_dir / "downloads",
            timeout_seconds=delivery.download_timeout_seconds,
            expiry=expiry,
            limits=limits,
        )
        # Downloads a stop left behind (before any request: none is running).
        left_over = await anyio.to_thread.run_sync(download_first.clear_left_over)
        if left_over:
            log.info("startup: %d download(s) a stop left behind removed", left_over)
        self.spawn(expiry.run)
        # (The add-ons too: a catalog may be one of theirs.)
        inner = self._catalog or build_catalog(
            settings.catalog, resolver=self.resolver, addons=sources
        )
        # Catalog covers: the artwork of items shown (also kept on disk), and resized
        # covers on disk.
        kept = settings.state_dir / "artwork" / "index.sqlite3"  # a cache, like the covers
        artwork = ArtworkIndex(path=kept, spawn=self.spawn)
        covers = (
            ArtworkCache(
                settings.state_dir / "artwork",
                max_bytes=settings.catalog.artwork_cache_mb * 2**20,
                max_age_seconds=settings.catalog.artwork_cache_days * 86400,
                spawn=self.spawn,
            )
            if settings.catalog.artwork_cache_mb > 0
            else None
        )
        catalog = (
            CachedCatalog(inner, ttl=settings.catalog.cache_seconds, index=artwork)
            if inner
            else None
        )
        if catalog is not None:
            self._opened.append(catalog.aclose)
        listening = Listening(memory_seconds=delivery.prefetch_memory_seconds)

        # The song a client says it plays never waits behind the user's fetches.
        # ... and its add-on requests under way go first at the add-ons' limits.
        def playing_now(user: str, key: str) -> None:
            download_first.promote(user, key)
            deliverer.promote(key)

        listening.on_current = playing_now
        download_first.current = lambda user, key: listening.current_for(user) == key
        warm = WarmAhead(
            deliverer,
            listening,
            UpcomingSongs(store, catalog),
            delay_seconds=delivery.warm_ahead_delay_seconds,
            jobs=delivery.warm_ahead_jobs,
        )
        ahead = AheadGate(
            listening,
            window_seconds=delivery.ahead_window_seconds,
            # A fetch ahead waits for its turn while its routing still fits the wait cap.
            wait_seconds=ahead_wait(delivery.budget_seconds, delivery.max_wait_seconds),
        )
        interceptor = Interceptor(
            store,
            deliverer,
            download_first,
            navidrome,
            self.upstream,
            warm=warm,
            ahead=ahead,
            recordings=recordings,
            limits=limits,
        )
        self.proxy.handlers.update(interceptor.handlers())
        self.proxy.path_interceptors["/share/"] = interceptor.share
        library_artists = LibraryArtists(
            navidrome, library_id=settings.navidrome.library_id, spawn=self.spawn
        )
        twins = Twins(settings.catalog.twins)
        # The catalog artists clients were shown, by name (getTopSongs names artists).
        shown = ShownArtists()
        views = VirtualViews(
            catalog,
            store,
            self.checker,
            library_artists=library_artists,
            twins=twins,
            artwork=artwork,
            covers=covers,
            shown=shown,
        )
        commits = Commits(
            catalog,
            engine,
            navidrome,
            views.materialized,
            artwork_size=settings.catalog.artwork_size,
            jukebox_enabled=interceptor.jukebox_enabled,
            server=lambda: self.checker.server,
            play=interceptor.catalog_play,
            committed=library_artists.forget,
        )
        views.sizes = CoverSizes(settings.catalog.cover_sizes)
        prefetch = (
            CoverPrefetch(
                covers,
                catalog,
                artwork,
                views.sizes,
                items=settings.catalog.prefetch_covers,
                parallel=settings.catalog.prefetch_parallel,
                burst_pages=settings.catalog.prefetch_burst_pages,
                burst_seconds=settings.catalog.prefetch_burst_seconds,
                spawn=self.spawn,
            )
            if covers is not None and catalog is not None
            else None
        )
        views.prefetch = prefetch
        virtual = views.handlers()
        self.proxy.handlers.update(virtual)
        # Catalog IDs in methods without a handler of their own.
        catalog_ids = CatalogIds(
            views.materialized,
            catalog,
            library_artists=library_artists,
            server=lambda: self.checker.server,
        )
        views.native = catalog_ids.native
        self.proxy.fallback = catalog_ids.handle
        search = settings.search
        additions = CatalogAdditions(
            catalog,
            store,
            self.upstream,
            navidrome=navidrome,
            min_query_length=search.min_query_length,
            budget_seconds=search.budget_seconds,
            artist_pages=search.artist_pages,
            library_id=settings.navidrome.library_id,
            spawn=self.spawn,
            cache_seconds=settings.catalog.cache_seconds,
            library_artists=library_artists,
            discographies=Discographies(
                store, max_age_seconds=search.discography_max_age_days * 86400
            ),
            artist_sync=Bursts(search.artist_sync_pages, search.artist_sync_seconds),
            search_guard=Bursts(
                search.guard_searches,
                search.guard_window_seconds,
                pause_seconds=search.guard_pause_seconds,
            ),
            # Song-only searches have their own, tight enough that a client's burst of
            # per-track lookups trips it at once.
            song_search_guard=Bursts(
                search.song_burst_searches,
                search.song_burst_seconds,
                pause_seconds=search.song_burst_pause_seconds,
            ),
            # ... and song-only searches sent one at a time: the search guard's settings.
            song_rate_guard=Bursts(
                search.guard_searches,
                search.guard_window_seconds,
                pause_seconds=search.guard_pause_seconds,
            ),
            twins=twins,
            artwork=artwork,
            song_only_additions=search.song_only_additions,
            song_settle_seconds=search.song_settle_seconds,
            # Top songs of artists not in the library: a client asking for many has
            # the artist pages' sync settings, counted apart.
            top_sync=Bursts(search.artist_sync_pages, search.artist_sync_seconds),
            shown=shown,
            server=lambda: self.checker.server,
        )
        additions.prefetch = prefetch
        self.proxy.handlers.update(
            additions.handlers(get_artist=virtual["getArtist"], ids=catalog_ids.handle)
        )
        fills: Fills | None = None
        library_pass: LibraryPass | None = None
        if catalog is not None and settings.fill.enabled:
            fill = settings.fill
            fills = Fills(
                store,
                navidrome,
                engine,
                Matcher(catalog, twins=twins),
                scope=catalog_scope(catalog),
                budget_seconds=fill.open_budget_seconds,
                retry_seconds=fill.retry_hours * 3600,
                spawn=self.spawn,
                syncs=Bursts(fill.sync_albums, fill.sync_seconds),
                pause_seconds=fill.background_pause_seconds,
                policy=FillPolicy(fill.auto_min_songs, fill.auto_min_share),
            )
            # Owned albums shown complete from their first view, filled on first use.
            albums = CompleteAlbums(fills, self.upstream, library_artists=library_artists)
            self.proxy.handlers["getAlbum"] = albums.wrap(self.proxy.handlers.get("getAlbum"))
            for method in LISTS if fill.complete_lists else ():  # ... also in lists
                listed = self.proxy.handlers.get(method, self.proxy.fallback)
                self.proxy.handlers[method] = albums.wrap_list(method, listed)
            commits.owned = fills
            catalog_ids.owner = views.owner = fills.owner
            views.album_of = navidrome.album
            additions.exposed = fills.exposed
            # The pass has a small cache of its own: its thousands of lookups must not push
            # listeners' answers out of the shared one.
            own = CachedCatalog(
                catalog.inner, ttl=settings.catalog.cache_seconds, maxsize=64, remember=0
            )
            library_pass = LibraryPass(
                fills,
                navidrome,
                Matcher(PacedCatalog(own, fill.pass_requests_per_second), twins=twins),
                mode=fill.library_pass,  # a dry run: nothing fills automatically
                start_seconds=fill.pass_start_seconds,
                interval_seconds=fill.pass_interval_hours * 3600,
                pause_seconds=fill.background_pause_seconds,
            )
            self.spawn(library_pass.run)
        # An action (the dashboard's): the release as the catalog has it now, past its cache.
        refresh = Refresh(store, navidrome, engine, inner) if inner is not None else None
        # Unused releases taken out again ...
        clean = settings.cleanup
        if settings.navidrome.usage_export_path and settings.navidrome.database_path:
            log.warning(
                "[navidrome] usage_export_path and database_path are both set: the usage"
                " export is read, database_path is not used (remove it, and the mount)"
            )
        cleanup = Cleanup(
            engine,
            database=settings.database_path,
            usage=configured(
                settings.navidrome.usage_export_path, settings.navidrome.database_path
            ),
            mode=clean.mode,
            unused_days=clean.unused_days,
            catalog_albums=clean.catalog_albums,
            fills=clean.fills,
            filler=fills,
            writes=writes,
            changed=library_artists.forget,
            spawn=self.try_spawn,
        )
        self.spawn(cleanup.run)
        # ... by a use of their old IDs, while views, covers and plain streams of them write
        # nothing.
        old_ids = OldIds(
            engine,
            commits=commits,
            fills=fills,
            changed=library_artists.forget,
            jukebox_enabled=interceptor.jukebox_enabled,
        )
        self.proxy.before = old_ids
        interceptor.use_old_id = old_ids.use  # a stream that needs converting
        for method in COMMITS:
            self.proxy.handlers[method] = commits.wrap(method, self.proxy.handlers.get(method))
        reports = Reports(store, listening, enabled=lambda: deliverer.settings.warm_ahead_depth > 0)
        for method in REPORTS:
            self.proxy.handlers[method] = reports.wrap(method, self.proxy.handlers.get(method))
        removed = RemovedViews(engine, self.upstream, views=views, artwork=artwork)
        for method in ("getAlbum", "getMusicDirectory", "getCoverArt"):
            self.proxy.handlers[method] = removed.wrap(method, self.proxy.handlers.get(method))
        self.services = Services(
            store,
            navidrome,
            scans,
            engine,
            sources,
            deliverer,
            download_first,
            interceptor,
            catalog,
            views,
            commits,
            additions,
            fills,
            library_pass,
            refresh,
            cleanup,
            old_ids,
            removed,
        )
        self.dashboard.attach(store, settings, problems)

    async def shutdown(self) -> None:
        """Close what startup opened, the last opened first (the add-ons' measurements are
        written before the database closes); a step that fails or hangs (each has a time
        bound) does not keep the others open. Shielded: a canceled stop still closes."""
        closing: list[Callable[[], Awaitable[None]]] = [self.dashboard.aclose]
        closing += reversed(self._opened)
        self._opened = []
        if self.restarting:
            # A restart cuts open requests off, and a file operation that one of them left
            # in a thread may still be running. The writer's lock is kept until the process
            # is gone - it ends with it -, so that no other writer comes beside that thread.
            closing = [close for close in closing if close != self._lock]
        for close in [*closing, self.upstream.aclose]:
            with anyio.move_on_after(CLOSE_SECONDS, shield=True) as bound:
                try:
                    await close()
                except Exception as exc:
                    log.warning("shutdown: %s failed: %s", _name(close), type(exc).__name__)
            if bound.cancelled_caught:
                log.warning("shutdown: %s did not end within %gs", _name(close), CLOSE_SECONDS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        await self.proxy(scope, receive, send)

    @property
    def can_restart(self) -> bool:
        """Whether a restart can be asked for: something starts Shijhon again here."""
        return self.restarter is not None

    def restart_problem(self) -> str | None:
        """Why a restart now would not bring Shijhon back where it is (None: nothing known
        against it)."""
        return self.restart_check() if self.restart_check is not None else None

    def begin_restart(self) -> bool:
        """The dashboard's "Restart Shijhon": the same orderly stop a stop signal gives;
        the process then exits, and whatever runs it starts it again (the saved settings
        apply from that start). Asked for again while one is under way: nothing more.
        False: not possible here, or not begun (Shijhon is starting or stopping)."""
        if self.restarter is None or self.restarting or self._stopping():
            return False
        self.restarting = self.try_spawn(self._restart)
        return self.restarting

    def _stopping(self) -> bool:
        return self.stopping is not None and self.stopping()

    async def _restart(self) -> None:
        """A moment for the answer to reach the browser; then a library write under way
        (new placeholders being written) ends first - but not for longer than a stop would
        give it -, and the server stops."""
        restarter = self.restarter
        try:
            await anyio.sleep(RESTART_AFTER)
            services = self.services
            with anyio.move_on_after(RESTART_WRITE_WAIT) as waited:
                if services is not None:
                    await services.engine.idle()
            if waited.cancelled_caught:
                log.warning(
                    "restart: a library write did not end within %gs; restarting (the next"
                    " start puts right what it leaves)",
                    RESTART_WRITE_WAIT,
                )
            # (A stop that came meanwhile stays a stop: no restart on top of it.)
            if self._stopping() or restarter is None or restarter() is False:
                self.restarting = False
                return
            log.info("restart: stopping at the dashboard's request")
        except BaseException:
            self.restarting = False  # (not begun after all: it can be asked for again)
            raise

    def spawn(self, work: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Start background work (warm-ahead, preparation requests) for the app's lifetime."""
        self.try_spawn(work)

    def try_spawn(self, work: Callable[[], Coroutine[Any, Any, None]]) -> bool:
        """As ``spawn``; False when the app is not running (the work is dropped). A failure
        of the work is its own: logged, it never ends the other background work."""
        if self._background is None:
            return False
        self._background.start_soon(self._contained, work, name=_name(work))
        return True

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        stopping = False
        try:
            async with anyio.create_task_group() as background:
                self._background = background
                try:
                    stopping = await self._lifespan_messages(receive, send)
                finally:
                    # Background work ends with the app; shutdown runs outside this group
                    # so that canceling it cannot cancel the shutdown itself.
                    self._background = None
                    background.cancel_scope.cancel()
        finally:
            # Whatever ended the lifespan (a stop, a failed startup, an error): what startup
            # opened is closed, also while the lifespan is being canceled.
            await self.shutdown()
        if stopping:
            await send({"type": "lifespan.shutdown.complete"})

    async def _lifespan_messages(self, receive: Receive, send: Send) -> bool:
        """Handle startup; return True when shutdown is requested (False: startup failed)."""
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self.startup()
                except Exception as exc:
                    log.exception("startup failed")
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return False  # what it opened is closed by the lifespan
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                return True

    async def _contained(self, work: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """One piece of background work: an exception ends it alone (logged by its type
        only: messages may carry an add-on's link; an error line a minute at most for each
        kind of work), never the app's other background work - the library pass, the
        cleanup, expiry, fills, warm-ahead, cover prefetch."""
        try:
            await work()
        except Exception as exc:
            name, now = _name(work), time.monotonic()
            again = now - self._reported.get(name, float("-inf")) < REPORT_SECONDS
            if not again:
                if len(self._reported) > 1024:
                    self._reported.clear()
                self._reported[name] = now
            level = logging.DEBUG if again else logging.ERROR
            log.log(level, "background work %s failed: %s", name, type(exc).__name__)
            # Where, for a debug log: the frames only - never the exception's message.
            where = "".join(traceback.format_tb(exc.__traceback__))
            log.debug("background work %s failed at:\n%s", name, where)


def _name(work: Callable[..., Any]) -> str:
    return getattr(work, "__qualname__", None) or type(work).__name__


def create_app(settings: Settings, *, resolver: Resolver = system_resolver) -> ShijhonApp:
    return ShijhonApp(settings, resolver=resolver)
