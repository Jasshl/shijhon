"""Configuration: a TOML file plus ``SHIJHON_*`` environment overrides.

Nested keys use a double underscore in the environment, for example
``SHIJHON_NAVIDROME__URL``. Secrets can be given inline or as ``*_file`` paths so that
deployments can keep them out of the configuration file.
"""

from __future__ import annotations

import copy
import functools
import ipaddress
import os
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    create_model,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from shijhon.catalog import plugin


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    # A request body that is not a form (the JSON client profile of getTranscodeDecision)
    # is read for the handlers up to this size; a larger one streams through behind them.
    # Forms are read as far as Navidrome reads them (10 MiB), whatever this says.
    max_parsed_body_bytes: int = 1024 * 1024
    # How long a successful credential check is reused (seconds).
    credential_cache_seconds: float = 60.0
    # Navidrome's reverse-proxy user header (its ExtAuth.UserHeader). Never passed on to
    # Navidrome (nor Remote-User, its default) unless reverse_proxy_auth is on: then it goes
    # on from trusted_proxies only - users signed in by the proxy in front.
    reverse_proxy_user_header: str = "Remote-User"
    reverse_proxy_auth: bool = False
    # The dashboard under /shijhon/ (Navidrome admins only). Off: that path goes to Navidrome
    # like any other.
    dashboard: bool = True
    # Whether something starts Shijhon again when it exits (a service manager that restarts
    # it): the dashboard then offers "Restart Shijhon", which stops Shijhon with an exit
    # status that is not 0. Not needed in a container, where Shijhon is the first process
    # and the container's restart policy does it.
    restart_by_supervisor: bool = False
    # Reverse proxies in front of Shijhon (addresses or networks): for them the last
    # address in X-Forwarded-For that is not one of them is the client's - the address
    # Navidrome's login limits count and the dashboard's sign-in limit; they must
    # append to X-Forwarded-For. Loopback by default; add a private network (e.g.
    # "172.16.0.0/12" for Docker) when the proxy runs there.
    trusted_proxies: list[str] = Field(default_factory=lambda: ["127.0.0.0/8", "::1/128"])

    @field_validator("trusted_proxies")
    @classmethod
    def _networks(cls, value: list[str]) -> list[str]:
        for item in value:
            ipaddress.ip_network(item, strict=False)  # ValueError names the bad entry
        return value


class NavidromeSettings(BaseModel):
    url: str = "http://127.0.0.1:4533"
    # Service account (Navidrome admin) used for scans and verification.
    user: str = ""
    password: SecretStr | None = None
    password_file: Path | None = None
    # Library that holds the owned files and the placeholder folder.
    library_id: int = 1
    # The library root as Shijhon sees it on disk.
    library_path: Path | None = None
    # Client name Shijhon uses for its own Subsonic calls.
    client_name: str = "shijhon"
    timeout_seconds: float = 30.0
    # Navidrome's Scanner.PurgeMissing, when its configuration cannot be read (Navidrome's
    # DevUIShowConfig is off): placeholders are written only with "never".
    purge_missing: str = ""
    # Whether anyone uses a placeholder (every user's favorites, plays, playlists,
    # queues...): without one of these two, no cleanup.
    # The usage export: a small file with only what the cleanup reads, made beside
    # Shijhon by `shijhon usage-export` (the Compose file's "usage-export" service, or a
    # timer on the host) - Shijhon then never sees Navidrome's database.
    usage_export_path: Path | None = None
    # Or Navidrome's database itself (navidrome.db), mounted read only: it also holds
    # Navidrome's secrets and its users' passwords, which Shijhon then can read.
    # With both set the export is read (said at startup): a deployment moving from the
    # one to the other is not kept from starting by it.
    database_path: Path | None = None

    def service_password(self) -> str:
        if self.password is not None:
            return self.password.get_secret_value()
        if self.password_file is not None:
            return self.password_file.read_text().strip()
        return ""


class PlaceholderSettings(BaseModel):
    # Folder inside the library root that holds all placeholders.
    folder: str = "_shijhon"


class DeliverySettings(BaseModel):
    """Defaults only: every source setting is meant to be edited in the dashboard."""

    model_config = ConfigDict(allow_inf_nan=False)

    # Time to first audio at byte zero, including one fallback source. With
    # primary_first routing the primary may use all of it; the fallbacks then get their own.
    budget_seconds: float = Field(default=9.0, gt=0, le=600)
    max_attempts: int = Field(default=2, ge=1, le=10)
    pin_ttl_seconds: float = Field(default=1800.0, gt=0)
    cooldown_seconds: float = Field(default=30.0, ge=0)
    seek_timeout_seconds: float = Field(default=15.0, gt=0)
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    download_timeout_seconds: float = Field(default=180.0, gt=0)
    # "ordered" tries sources in order. "ready_first" uses the first source that says
    # it can deliver now, else reliable_source. "primary_first" starts primary_source
    # at once while the others are asked whether they can deliver now: after
    # primary_budget_seconds without a first byte, a source that can takes over; otherwise
    # the primary gets up to budget_seconds (or its own budget), then the fallbacks, ordered by
    # their recent attempts (reliable_source first until they are measured).
    routing: Literal["ordered", "ready_first", "primary_first"] = "ordered"
    reliable_source: str = ""
    primary_source: str = ""
    primary_budget_seconds: float = Field(default=4.5, gt=0)
    # The primary cools down (cooldown_seconds) after this many timeouts, or this many
    # switches away from it to a source that can deliver now, without a delivery in between.
    primary_cooldown_timeouts: int = Field(default=2, ge=1)
    primary_cooldown_switches: int = Field(default=3, ge=1)
    # Any add-on cools down (cooldown_seconds) after this many errors in a row (an HTTP 5xx,
    # no connection, a broken answer; not a miss, a refusal or a slow answer) without a
    # delivery in between, and again after each further one until it delivers. 0: off.
    cooldown_errors: int = Field(default=3, ge=0)
    # A client's whole wait for the first byte, whatever the routing and the add-ons' own
    # budgets: below typical client request timeouts.
    max_wait_seconds: float = Field(default=30.0, gt=0)
    availability_timeout_seconds: float = Field(default=2.0, gt=0)
    # Under primary_first, the reliable source is looked up (never streamed) when the
    # primary's lookup has not answered after this long, and at once when the primary is
    # skipped after a remembered miss. 0: with every primary attempt; the byte-zero
    # budget or more: never while the primary is asked.
    reliable_lookup_after_seconds: float = Field(default=0.5, ge=0)
    # A recording the primary did not have is not asked for there again for this long; for a
    # release it lacked a song of, the likely fallback is looked up at once for this long,
    # while the primary still starts first (0: off).
    primary_miss_hours: float = Field(default=24.0, ge=0)
    primary_release_miss_minutes: float = Field(default=60.0, ge=0)
    # Delivered audio whose length (read from its first bytes: FLAC, MP3, MP4 with its index
    # first) differs from the catalog's by more than the larger of these is another
    # recording: not used, the next source is tried, and that add-on is not asked for the
    # recording again for a while. Both 0: no check.
    length_tolerance_seconds: float = Field(default=10.0, ge=0)
    length_tolerance_percent: float = Field(default=5.0, ge=0, le=100)
    # A new routing of a song whose routing failed a moment ago (a client's retry) skips the
    # add-ons that failed for it or lacked it within this long, and goes to the others;
    # after it every add-on is tried again. 0: every retry tries them all.
    retry_skip_seconds: float = Field(default=60.0, ge=0)
    prepare_when_not_ready: bool = False  # ask one source that said "not now" to prepare
    # Download-first audio in place of placeholders goes back to the silent placeholder
    # after this many days without use, and the least recently used first past this many
    # GB in all (0: never by age, no size limit).
    delivered_days: float = Field(default=30.0, ge=0)
    delivered_gb: float = Field(default=10.0, ge=0)
    # Per user (0: no limit): songs being looked up at the add-ons at
    # once (more wait their turn), download-first fetches at once (more wait), and
    # download-first fetches an hour (that many at once, then one every hour / that number,
    # in order: a large sync slows down instead of failing). The song being played never
    # waits behind them.
    user_routings: int = Field(default=4, ge=0)
    user_downloads: int = Field(default=2, ge=0)
    user_downloads_per_hour: int = Field(default=120, ge=0)
    # ... of which this many may start at once after a quiet while (a first offline
    # sync starts with a few downloads, not with the whole hour's); then one every hour /
    # user_downloads_per_hour. 0: the whole hour's allowance at once.
    user_download_burst: int = Field(default=4, ge=0)
    # Per add-on, whoever asks - all users, their clients and
    # Shijhon's own background work together: the API requests one add-on is sent a second
    # (its manifest, lookups, link requests, availability checks; 0: no limit), how many may
    # go at once after a quiet moment, and the audio requests being opened there at once
    # (until their answers start; 0: no limit). The song being played goes first. Add-ons
    # at one address (scheme, host and port) share them. An add-on can have its own
    # ([[addons]] requests_per_second, request_burst, audio_openings; the dashboard's
    # Add-ons page): raise them for an add-on of your own, not for someone else's.
    addon_requests_per_second: float = Field(default=2.0, ge=0, le=1000)
    addon_request_burst: int = Field(default=4, ge=1, le=1000)
    addon_audio_openings: int = Field(default=4, ge=0, le=1000)
    # Warm-ahead: open the next songs when one starts playing - the next ones in the
    # client's saved queue, else in its album.
    warm_ahead_depth: int = Field(default=2, ge=0, le=20)
    # Warm-ahead jobs at the add-ons at once, for all listeners together: more wait
    # their turn (a job opens its songs one after the other).
    warm_ahead_jobs: int = Field(default=2, ge=1, le=20)
    warm_ahead_budget_seconds: float = Field(default=60.0, gt=0)
    # It starts this long after a play's first byte, once the client's reports and its own
    # requests around the start are in.
    warm_ahead_delay_seconds: float = Field(default=2.0, ge=0)
    # A client that fetches upcoming songs itself gets no warm-ahead for this long; a
    # "now playing" report counts for this long.
    prefetch_memory_seconds: float = Field(default=600.0, gt=0)
    # A play at byte zero is a fetch ahead when the same client started another song within
    # this time and does not report this one as playing: the primary alone, one at a
    # time per client, after the song being played. 0: every play is routed alike.
    ahead_window_seconds: float = Field(default=0.5, ge=0)
    # Links to a DASH manifest (segments rather than one file): the segments are joined
    # into one file, which is then served like any other. Needs ffmpeg on the PATH (without
    # it they are not played, and the next add-on is asked).
    dash: bool = True
    # Of several qualities in one manifest, the highest within this range: kbit/s, and
    # lossless above them all (a bitrate within 5 % of a bound counts as at it). A song with
    # no quality in it is not played from that add-on: the next one is asked. "any" to
    # "lossless": the best there is.
    dash_quality_from: Literal["any", "128", "192", "256", "320", "lossless"] = "any"
    dash_quality_to: Literal["128", "192", "256", "320", "lossless"] = "lossless"
    # "at_once": a DASH song plays from its first segments on, served as one MP4 file of
    # them (each segment's size asked for first); "complete": once all are joined into a
    # FLAC, M4A or MP3. Copies for the library are joined files either way.
    dash_start: Literal["at_once", "complete"] = "at_once"
    dash_segments_at_once: int = Field(default=4, ge=1, le=16)
    # DASH songs joined at once, for all listeners together, besides the songs being played
    # (they never wait, and count): more wait their turn. A join goes on after the request
    # that started it gave up, for up to max_wait_seconds, so that the app's retry gets it.
    dash_joins_at_once: int = Field(default=3, ge=1, le=20)
    # Joined files are kept for later plays and seeks, the least recently used going first
    # past this size (never one being played).
    dash_cache_mb: int = Field(default=1024, ge=0)

    @field_validator("dash_quality_from", "dash_quality_to", mode="before")
    @classmethod
    def _quality(cls, value: Any) -> Any:
        """(A bitrate may be written as a number: 320.)"""
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @model_validator(mode="after")
    def _check(self) -> DeliverySettings:
        order = ("any", "128", "192", "256", "320", "lossless")
        if order.index(self.dash_quality_from) > order.index(self.dash_quality_to):
            raise ValueError(
                f"delivery.dash_quality_from ({self.dash_quality_from}) is above"
                f" delivery.dash_quality_to ({self.dash_quality_to}): the range has no"
                " quality in it; lower the first or raise the second"
            )
        if self.max_wait_seconds < self.budget_seconds:
            raise ValueError(
                f"delivery.max_wait_seconds ({self.max_wait_seconds:g} s) is below the"
                f" byte-zero budget delivery.budget_seconds ({self.budget_seconds:g} s):"
                " a client would give up before one source had its chance; raise"
                " max_wait_seconds or lower budget_seconds"
            )
        return self


class CatalogSettings(BaseModel):
    """The catalog for search, artist pages and catalog albums. None is
    configured by default: ``kind`` selects a catalog adapter the installation provides
    (``shijhon.catalog.plugin``), whose own settings - its region, its credentials, its
    request rate - live in this section too (``catalog_settings(kind)``)."""

    model_config = ConfigDict(allow_inf_nan=False)

    kind: str = "none"  # "none", or the name of an installed catalog adapter
    cache_seconds: float = Field(default=3600.0, ge=0)  # catalog answers are reused this long
    timeout_seconds: float = Field(default=15.0, gt=0)
    # Cover images written for catalog-only albums (pixels).
    artwork_size: int = Field(default=1200, ge=100, le=4000)
    # Resized catalog covers are kept in the state directory (``artwork/``) this long, up
    # to this size (the oldest go first); 0: none kept on disk (a few in memory). The covers'
    # addresses are kept there too (``index.sqlite3``, small), whatever this says.
    artwork_cache_mb: int = Field(default=1024, ge=0)
    artwork_cache_days: float = Field(default=30.0, gt=0)
    # Catalog covers are fetched at the next of these sizes up from the one a client asks
    # for (image servers tend to answer their common sizes at once and render others first,
    # measured 0.35-0.6 s a cover; a cover kept at a common size also serves more requests);
    # empty: exactly the size asked for.
    cover_sizes: list[int] = Field(
        default_factory=lambda: [100, 150, 200, 250, 300, 400, 500, 600, 800, 1000, 1200]
    )
    # The covers of the first catalog items of an artist page or a search result are
    # fetched ahead, in the background, at the size the client asks for (known from its
    # cover requests), this many at a time; 0: none.
    prefetch_covers: int = Field(default=50, ge=0)
    prefetch_parallel: int = Field(default=4, ge=1)
    # A client shown this many pages with catalog items within the window is walking
    # pages, not looking at one: nothing is fetched ahead for it meanwhile.
    prefetch_burst_pages: int = Field(default=4, ge=2)
    prefetch_burst_seconds: float = Field(default=10.0, gt=0)
    # Clean and explicit versions of one album or song: show the explicit one, the
    # clean one, or both. A catalog without such flags: one of them.
    twins: Literal["explicit", "clean", "both"] = "explicit"

    @field_validator("kind")
    @classmethod
    def _installed(cls, value: str) -> str:
        if value != plugin.NONE:
            plugin.adapter(value)  # ValueError: not installed (it says what is)
        return value

    @field_validator("cover_sizes")
    @classmethod
    def _sizes(cls, value: list[int]) -> list[int]:
        """In order, each once (the file, the environment and the dashboard agree)."""
        if any(size < 1 or size > 1200 for size in value):
            raise ValueError("cover sizes are from 1 to 1200 px")
        return sorted(set(value))


CORE_CATALOG_KEYS = frozenset(CatalogSettings.model_fields)


def catalog_settings(kind: str) -> type[CatalogSettings]:
    """The ``[catalog]`` section's settings with the adapter ``kind``: Shijhon's own and
    the adapter's declared ones, as one model (so the file, the environment and the
    dashboard treat them alike). Shijhon's own alone for "none", an adapter without
    settings, or one that is not installed (``kind`` is then refused when validated)."""
    adapter = plugin.find(kind) if kind != plugin.NONE else None
    if adapter is None or adapter.settings is None:
        return CatalogSettings
    return _with_adapter(kind, adapter)


@functools.cache
def _with_adapter(kind: str, adapter: plugin.Adapter) -> type[CatalogSettings]:
    assert adapter.settings is not None
    bases: tuple[type[BaseModel], ...] = (CatalogSettings, adapter.settings)
    model = create_model(f"CatalogSettings[{kind}]", __base__=bases)
    assert issubclass(model, CatalogSettings)
    return model


def one_choices(kind: str) -> tuple[tuple[str, ...], ...]:
    """The catalog settings that are one choice each (the adapter's declaration)."""
    adapter = plugin.find(kind)
    return adapter.one_choice if adapter is not None else ()


class SearchSettings(BaseModel):
    """Catalog additions to search results and artist pages."""

    # Shorter queries get Navidrome's answer only (search-as-you-type's first keystrokes).
    min_query_length: int = Field(default=3, ge=1)
    # The longest a search result or an artist page waits for the catalog: answered
    # as soon as the catalog has replied, and without the additions when it has not by
    # then (the lookup goes on: once it has succeeded, the same request has them). A search
    # answer may wait a second more for its entries' artist items (views/additions.py).
    budget_seconds: float = Field(default=8.0, gt=0, le=120)
    artist_pages: bool = True  # add the catalog releases missing from a library artist
    # Artist discographies are saved: a saved list answers at once and is refreshed
    # in the background when older than this.
    discography_max_age_days: float = Field(default=7.0, gt=0)
    # A client opening this many different artist pages without a saved discography within
    # the window is syncing: those pages get the library's answer alone.
    artist_sync_pages: int = Field(default=10, ge=2)
    artist_sync_seconds: float = Field(default=10.0, gt=0)
    # Search guard: a client sending this many different searches the catalog
    # would be asked for within the window gets the library's answers alone for a while.
    # Generous: typing never gets there.
    guard_searches: int = Field(default=60, ge=2)
    guard_window_seconds: float = Field(default=20.0, gt=0)
    guard_pause_seconds: float = Field(default=60.0, ge=0)
    # Advanced: catalog additions for song-only searches (no artists or albums asked for,
    # e.g. a client's "Tracks" search). On: an isolated one gets them, one catalog
    # lookup at a time; off: the library answers them all.
    song_only_additions: bool = True
    # Song-only searches from one client arriving in a burst - this many different ones
    # within the window, e.g. a client looking up each track of an album it opened - are
    # answered from the library alone until it has sent none for the pause. A term
    # that extends or shortens the previous one is the same search (typing). Song-only
    # searches sent one at a time also have the search guard's settings above.
    song_burst_searches: int = Field(default=4, ge=2)
    song_burst_seconds: float = Field(default=0.5, gt=0)
    song_burst_pause_seconds: float = Field(default=2.0, ge=0)
    # A song-only search waits this long before asking the catalog: the next search of a
    # burst arrives within milliseconds and supersedes it.
    song_settle_seconds: float = Field(default=0.15, ge=0)


class FillSettings(BaseModel):
    """Filling partially owned albums."""

    enabled: bool = True  # automatic for certain matches; others go to the review list
    # How long a view of an album never matched waits for its match (it goes on in the
    # background after, and the view gets the owned songs only).
    open_budget_seconds: float = Field(default=3.0, gt=0)
    # A client viewing this many different never-matched albums within the window is
    # syncing (clients fetch every album they list): those are matched in the background.
    sync_albums: int = Field(default=10, ge=2)
    sync_seconds: float = Field(default=10.0, gt=0)
    retry_hours: float = Field(default=1.0, gt=0)  # after a failed match (then doubling)
    # Between background matches (albums shown by searches and artist pages): about one
    # catalog request a second.
    background_pause_seconds: float = Field(default=5.0, ge=0)
    # The paced pass over the whole library: "dry_run" only lists what it would
    # do (kept in the database; `shijhon matches` prints it), "on" fills, "off" does
    # neither. It looks at every album; those below the fill policy are only matched, so a
    # view shows them complete at once. During a dry run nothing is filled
    # automatically (views, searches, artist pages and syncs only match): only first use
    # fills. "on" and "off" leave automatic fills to the fill policy below.
    library_pass: Literal["off", "dry_run", "on"] = "dry_run"  # noqa: S105 (not a password)
    # An owned album shown complete carries the complete album's song count and
    # length in album lists too (getArtist, search3, getAlbumList2, getStarred2);
    # off: lists keep Navidrome's counts until the fill.
    complete_lists: bool = True
    # Fill policy: automatic fills (the library pass, albums shown by searches and
    # artist pages, views) only when the owner has at least this many songs of the album,
    # or this share of its tracks; others are shown complete and filled on first use.
    # 1: fill every album.
    auto_min_songs: int = Field(default=3, ge=1)
    auto_min_share: float = Field(default=0.25, ge=0, le=1)
    pass_requests_per_second: float = Field(default=1.0, gt=0)  # catalog requests
    pass_start_seconds: float = Field(default=60.0, ge=0)  # after startup
    pass_interval_hours: float = Field(default=6.0, gt=0)  # then it looks for new albums


class CleanupSettings(BaseModel):
    """Taking unused releases out of the library again. Fixed rules:
    only whole releases; anything used even once is kept; a use of an old ID adds a release
    back.
    Downloaded audio has its own limits (``[delivery] delivered_days``, ``delivered_gb``)."""

    # "dry_run" only lists what it would take out (logged; `shijhon cleanup` prints it),
    # "on" takes it out, "off" does neither. Needs [navidrome] usage_export_path (or
    # database_path).
    mode: Literal["off", "dry_run", "on"] = "off"
    # A release nobody has used is taken out this many days after it was added (checked
    # daily).
    unused_days: float = Field(default=30.0, gt=0)
    # Which releases: catalog albums added to the library (a commit), and the songs added
    # to partly owned albums (fills; the album is then shown complete again and filled on
    # its next use, never automatically).
    catalog_albums: bool = True
    fills: bool = True


class AddonSettings(BaseModel):
    """An audio add-on (the add-on protocol) kept by the configuration file."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    base_url: SecretStr  # an add-on's URL often carries its configuration or a token
    # "loopback" for a worker on this machine, "private" for a LAN host.
    reach: Literal["public", "loopback", "private"] = "public"
    # The add-on's declared settings (may be secret too).
    settings: dict[str, Any] = Field(default_factory=dict, repr=False)
    # Own time to first audio, e.g. for a worker that prepares complete files.
    budget_seconds: float | None = Field(default=None, gt=0, le=600)
    # Own limits on what Shijhon sends it (not given: [delivery] addon_requests_per_second,
    # addon_request_burst, addon_audio_openings): API requests a second (0: no limit), how
    # many at once after a quiet moment, audio requests being opened at once (0: no limit).
    requests_per_second: float | None = Field(default=None, ge=0, le=1000)
    request_burst: int | None = Field(default=None, ge=1, le=1000)
    audio_openings: int | None = Field(default=None, ge=0, le=1000)


class Settings(BaseSettings):
    # Validation errors never repeat the input: it holds passwords, tokens and add-on URLs.
    model_config = SettingsConfigDict(
        env_prefix="SHIJHON_",
        env_nested_delimiter="__",
        extra="forbid",
        hide_input_in_errors=True,
        # An empty variable (Compose's ${VAR:-}) sets nothing.
        env_ignore_empty=True,
    )
    # Which settings the environment set (section -> key -> the variable), filled by
    # load_settings: the dashboard shows them locked (they win over its values).
    _from_environment: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)

    @property
    def from_environment(self) -> dict[str, dict[str, str]]:
        return self._from_environment

    state_dir: Path = Path("var")
    log_level: str = "INFO"
    # Loggers logged at debug level whatever ``log_level`` says, e.g. ["shijhon.covers"]
    # (one line per catalog cover request: sizes, cache, times).
    log_debug: list[str] = Field(default_factory=list)
    server: ServerSettings = Field(default_factory=ServerSettings)
    navidrome: NavidromeSettings = Field(default_factory=NavidromeSettings)
    placeholders: PlaceholderSettings = Field(default_factory=PlaceholderSettings)
    delivery: DeliverySettings = Field(default_factory=DeliverySettings)
    catalog: CatalogSettings = Field(default_factory=CatalogSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    fill: FillSettings = Field(default_factory=FillSettings)
    cleanup: CleanupSettings = Field(default_factory=CleanupSettings)
    # Audio add-ons in the order they are tried. When given (even empty), the configuration
    # owns the list: at startup add-ons are added, updated and ordered to match it, and
    # stored ones it no longer names are disabled. Not given: the stored list is kept.
    addons: list[AddonSettings] | None = None

    @model_validator(mode="after")
    def _check(self) -> Settings:
        folder = PurePosixPath(self.placeholders.folder)
        if not self.placeholders.folder or folder.is_absolute():
            raise ValueError("placeholders.folder must be a relative folder name")
        if ".." in folder.parts:
            raise ValueError("placeholders.folder must stay inside the library")
        if not folder.parts:  # ".": placeholders would lie among the owned music
            raise ValueError("placeholders.folder must be a folder of its own in the library")
        # As its parts ("./x", "x/" and "x//y" are "x" and "x/y"): what is a placeholder is
        # told by this prefix of its library path.
        self.placeholders.folder = "/".join(folder.parts)
        if self.addons is not None:
            names = [addon.name for addon in self.addons]
            if len(set(names)) != len(names):
                raise ValueError("addons: every add-on needs its own name")
        return self

    @property
    def database_path(self) -> Path:
        return self.state_dir / "shijhon.sqlite3"


@functools.cache
def _settings_class(catalog: type[CatalogSettings]) -> type[Settings]:
    """``Settings`` whose catalog section also has an adapter's settings."""
    if catalog is CatalogSettings:
        return Settings
    model: type[Settings] = create_model(
        "Settings", __base__=Settings, catalog=(catalog, Field(default_factory=catalog))
    )
    return model


def settings_class(kind: str) -> type[Settings]:
    return _settings_class(catalog_settings(kind))


def forget_adapters() -> None:
    """The adapters changed (``plugin.register``): their settings classes are made anew."""
    _with_adapter.cache_clear()
    _settings_class.cache_clear()


class _AnyCatalog(CatalogSettings):
    """The section as first read: whatever it holds is kept (the adapter is not known yet)."""

    model_config = ConfigDict(extra="allow")


class _FirstRead(Settings):
    catalog: _AnyCatalog = Field(default_factory=_AnyCatalog)


def _own_keys_only(data: dict[str, Any]) -> dict[str, Any]:
    """The file without its catalog adapter's settings (they are another kind's than the
    one in use, or the file names no kind)."""
    section = data.get("catalog")
    if not isinstance(section, dict):
        return data
    kept = {key: value for key, value in section.items() if key in CORE_CATALOG_KEYS}
    return {**data, "catalog": kept}


def load_settings(config_file: Path | None = None, **overrides: Any) -> Settings:
    """Load settings from ``config_file`` (TOML), the environment and ``overrides``. The
    catalog's kind is read first: its adapter's settings are part of ``[catalog]``.

    An adapter's settings belong to the kind they were written for: the file's to the kind
    the file names (when the environment or ``overrides`` name another, they are not read),
    the environment's to the configured kind. Adapter settings in a file that names no
    kind belong to no adapter: they are not read.

    The file is read once: whose its settings are and what they are come from that one
    reading (a file changed meanwhile cannot hand one kind's settings to another)."""
    file: dict[str, Any] = {}
    if config_file is not None:
        file = dict(TomlConfigSettingsSource(Settings, toml_file=config_file).toml_data)

    def read(model: type[Settings], data: dict[str, Any]) -> Settings:
        class _Settings(model):  # type: ignore[valid-type, misc]
            @classmethod
            def settings_customise_sources(
                cls,
                settings_cls: type[BaseSettings],
                init_settings: PydanticBaseSettingsSource,
                env_settings: PydanticBaseSettingsSource,
                dotenv_settings: PydanticBaseSettingsSource,
                file_secret_settings: PydanticBaseSettingsSource,
            ) -> tuple[PydanticBaseSettingsSource, ...]:
                sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
                if config_file is not None:
                    sources.append(InitSettingsSource(settings_cls, copy.deepcopy(data)))
                return tuple(sources)

        return model.model_validate(_Settings(**overrides).model_dump())

    first = read(_FirstRead, file)
    kind = first.catalog.kind
    model = settings_class(kind)
    section = file.get("catalog")
    named = section.get("kind") if isinstance(section, dict) else None
    # The file's adapter settings are the kind's the file names.
    theirs = kind != plugin.NONE and named == kind
    settings = read(model, file if theirs else _own_keys_only(file))
    environment = _environment(model, overrides)
    from_env = environment.get("catalog", {})
    # A choice (e.g. where a token comes from) is one choice: one given by the environment
    # replaces the file's.
    given = overrides.get("catalog")
    kept = set(from_env) | (set(given) if isinstance(given, dict) else set())
    cleared = {
        key: None
        for group in one_choices(kind)
        if from_env.keys() & set(group)
        for key in group
        if key not in kept
    }
    if cleared:
        settings = settings.model_copy(
            update={"catalog": settings.catalog.model_copy(update=cleared)}
        )
    settings._from_environment = environment
    return settings


def _environment(model: type[Settings], overrides: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The section settings the environment set and ``overrides`` did not (they win), each
    with the variable that set it - read the way the settings read it: any letter case, or
    a whole section as JSON. The variable as it is named in the environment."""
    values = EnvSettingsSource(model)()
    names = {name.lower(): name for name in os.environ}
    found: dict[str, dict[str, str]] = {}
    for section, keys in values.items():
        given = overrides.get(section)
        if section == "addons" and given is None:  # the whole list (JSON)
            found["addons"] = {"list": names.get("shijhon_addons", "SHIJHON_ADDONS")}
            continue
        if not isinstance(keys, dict) or (given is not None and not isinstance(given, dict)):
            continue
        for key in keys:
            if isinstance(given, dict) and key in given:
                continue
            found.setdefault(section, {})[key] = _variable(names, section, key)
    return found


def _variable(names: dict[str, str], section: str, key: str) -> str:
    """The variable that set ``section.key``, as it is named in the environment."""
    exact = names.get(f"shijhon_{section}__{key}")
    if exact is not None:
        return exact
    nested = sorted(
        name for low, name in names.items() if low.startswith(f"shijhon_{section}__{key}__")
    )
    if nested:  # a mapping given item by item
        return nested[0] if len(nested) == 1 else f"{nested[0]} (and {len(nested) - 1} more)"
    whole = names.get(f"shijhon_{section}")
    return f"{whole} (JSON)" if whole else f"SHIJHON_{section.upper()}__{key.upper()}"
