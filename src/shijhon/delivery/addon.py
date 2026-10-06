"""Client for add-ons speaking the add-on HTTP protocol (docs/development.md, "The add-on
protocol").

Used endpoints: ``/manifest.json``, ``/resolve-isrc``, ``/resolve`` (when declared) and
``/stream/{id}``; ``/search?q=`` to find a recording at an add-on that declares ``search``
but no ``resolve`` (``Addon.find``) - and, for an add-on whose catalog is in use
(``catalog.addon``), its catalog requests: ``/search?q=``, ``/album/{id}`` and
``/artist/{id}`` (declared as the resources ``search`` and ``catalog``). Shijhon also
understands one optional extension,
declared as the resource
``availability``: ``GET /availability?isrc=&title=&artist=&durationMs=`` answers
``{"available": true|false|null, "id": "<track id>", "preparing": bool}`` — can the add-on
deliver this recording now, without preparing it first? With ``prepare=true`` an add-on
that cannot may start preparing it for next time. The check must not change anything
unless asked to prepare, and must answer quickly.

Settings declared in the manifest are sent as query parameters on every request (manifest
defaults, overridden by the user's values).

Parsing is lenient, because real add-ons differ: numeric IDs are accepted, ``null`` or
empty optional fields are treated as absent, and the stream object may be wrapped. Every
error carries a short reason (never a URL or secret).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import anyio
import httpx

from shijhon.delivery import pacing
from shijhon.delivery.netpolicy import denied
from shijhon.delivery.pacing import AddonPace, Paces
from shijhon.matching.normalize import (
    close_duration,
    fold,
    same_artist,
    same_title,
    version_marker,
)

MAX_JSON_BYTES = 1024 * 1024
TRANSPORTS = {"none": "direct", "hls": "hls", "dash": "dash"}
# What an add-on with a catalog declares: its search, and its albums and artists.
CATALOG = ("search", "catalog")
MAX_ITEM_ID = 190  # ``catalog.model.ITEM_ID``: the longest ID an item can have
SEARCHED = 100  # a search's tracks compared at most, in its order, when finding a recording
# The longest title or artist of a search's track compared (as the catalog of an add-on
# reads them): a longer one is no track's, and never goes through the patterns.
MAX_WORDS = 500
SERVICE_TAG = 8  # characters of an item ID that say which service it is of
_PLAIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_HEX = frozenset("0123456789ABCDEF")
# An album title's mark of an edited (clean) version, besides the markers of track titles:
# "(Edited)", "[Edited Version]", "- Edited".
_EDITED = re.compile(
    r"[\(\[]\s*edited(?:\s+version)?\s*[\)\]]|\s+-\s+edited(?:\s+version)?\s*(?=$|[\(\[])",
    re.IGNORECASE,
)
_EXPLICIT_WORDS = {"true": True, "explicit": True, "1": True, "yes": True,
                   "false": False, "clean": False, "0": False, "no": False,
                   "notexplicit": False, "not explicit": False}  # fmt: skip


class AddonError(Exception):
    """kind: unavailable | rate_limited | expired | timeout | invalid (an answer about the
    item that Shijhon cannot use: an unexpected status, no URL) | broken (a malformed answer:
    not JSON, too large, unreadable) | denied | failed (an HTTP 5xx, no connection) |
    cooling (not asked: the add-on said "too many requests" and its time has not passed).
    ``retry_after``: the seconds a rate limit's ``Retry-After`` named (None: none, or not
    valid). ``shared``: another request's answer, waited for (a manifest read once for
    several first uses): its failure is counted where it was asked."""

    def __init__(
        self, kind: str, reason: str, retry_after: float | None = None, *, shared: bool = False
    ) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason
        self.retry_after = retry_after
        self.shared = shared


def text(value: Any) -> str | None:
    """Lenient optional string: numbers become strings, blanks become None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):  # (whatever its size: never through a float)
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):  # (an integer too large for a number)
        return None
    return result if math.isfinite(result) else None


def catalog_key(name: str) -> str:
    """The key of the catalog the add-on named ``name`` is: what tells its items
    from every other add-on's and catalog's (``sh.al.<key>.<id>``). Made from the name
    alone - its letters and digits, and a digest of the name as it is written (96 bits: two
    names do not share one) - and so known without asking the add-on: renamed, an add-on is
    another catalog."""
    slug = "".join(ch for ch in fold(name) if ch in _PLAIN)[:16]
    digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    return f"addon{slug}{digest}"


def item_id(own: Any) -> str | None:
    """An add-on's own ID as a catalog item's ID (letters, digits and dots): letters and
    digits as they are, every other byte as a dot and two hexadecimal digits ("a:b" is
    "a.3Ab"), so the add-on's ID can be read back (``own_id``) - exactly as it was sent: text
    is not trimmed (" a" is another ID than "a"). None: no ID, one too long to carry, one
    that is no text (a lone surrogate), or one that is no path segment ("." and "..": asked
    for, it would be another address of the add-on)."""
    value = own if isinstance(own, str) else text(own)
    if not value or value in (".", "..") or len(value) > MAX_ITEM_ID:
        return None
    try:
        out = "".join(
            ch if ch in _PLAIN else "".join(f".{byte:02X}" for byte in ch.encode()) for ch in value
        )
    except UnicodeEncodeError:
        return None
    return out if len(out) <= MAX_ITEM_ID else None


def own_id(item: str) -> str | None:
    """The add-on's own ID behind a catalog item's (``item_id``); None for anything that
    is not one - also another spelling of one ("a.41" for "aA"): an item has one ID."""
    out = bytearray()
    index = 0
    while index < len(item):
        ch = item[index]
        if ch in _PLAIN:
            out += ch.encode()
            index += 1
        elif ch == "." and len(pair := item[index + 1 : index + 3]) == 2 and set(pair) <= _HEX:
            out.append(int(pair, 16))
            index += 3
        else:
            return None
    try:
        own = out.decode()
    except UnicodeDecodeError:
        return None
    return own if own and item_id(own) == item else None


def service_tag(service: str) -> str:
    """The first characters of every item ID of an add-on's catalog: a digest of
    the service the add-on is - its manifest's ``id``. The catalog's key is the add-on's
    name; the tag ties an item to what answered under that name, so an add-on pointed at
    another service does not get the IDs of the one before (they are not found, and its
    audio is looked up)."""
    return hashlib.sha256(service.encode("utf-8", "surrogatepass")).hexdigest()[:SERVICE_TAG]


def tagged(service: str, own: Any) -> str | None:
    """The item ID for the add-on's own ID ``own``, of the service ``service`` (its
    manifest's ``id``): the service's tag, then the ID (``item_id``). None: no ID Shijhon
    can carry."""
    ident = item_id(own)
    if ident is None or SERVICE_TAG + len(ident) > MAX_ITEM_ID:
        return None
    return service_tag(service) + ident


def untagged(service: str, item: str) -> str | None:
    """The add-on's own ID behind an item ID of the service ``service`` (``tagged``); None
    for an item of another service, or anything else."""
    if item[:SERVICE_TAG] != service_tag(service):
        return None
    return own_id(item[SERVICE_TAG:])


def of_catalog(name: str, ref: str | None) -> bool:
    """Whether ``ref`` (a catalog track, "<key>:<id>") is an item of the catalog the
    add-on named ``name`` is."""
    return (ref or "").partition(":")[0] == catalog_key(name)


def own_track(name: str, service: str, ref: str | None) -> str | None:
    """The track ID the add-on named ``name`` itself has for a song: when the song
    came from that add-on's catalog (``ref``: its catalog track, "<key>:<id>") while it
    was the service it is now (``service``: its manifest's ``id``), its ID there is the
    add-on's own, and the add-on can be asked for its audio without a lookup. None: a song
    of another catalog, of another service under this name, or of none."""
    key, _, item = (ref or "").partition(":")
    # (A manifest without an ID says nothing about which service it is: looked up.)
    return untagged(service, item) if service and key == catalog_key(name) else None


def allows_downloads(value: Any) -> bool:
    """A manifest's ``allowDownloads``: False when it is 0, false or "0" (or "false") - the
    add-on asks not to be used for bulk downloads; True when it is absent, 1 or anything
    else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false")
    return True


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    resources: frozenset[str]
    defaults: dict[str, str] = field(default_factory=dict)
    # False: it asks not to be used for downloads (``allowDownloads``): Shijhon's
    # download-first work and its requests to prepare a song go to other add-ons.
    downloads: bool = True

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> Manifest:
        resources: set[str] = set()
        raw_resources = data.get("resources")
        for item in raw_resources if isinstance(raw_resources, list) else []:
            name = text(item.get("name")) if isinstance(item, dict) else text(item)
            if name:
                resources.add(name.lower())
        defaults: dict[str, str] = {}
        raw_settings = data.get("settings")
        for setting in raw_settings if isinstance(raw_settings, list) else []:
            if not isinstance(setting, dict):
                continue
            key = text(setting.get("key"))
            default = setting.get("default")
            if not key or key in ("q", "isrc") or default is None:
                continue
            defaults[key] = _setting_value(default)
        return cls(
            id=text(data.get("id")) or "",
            name=text(data.get("name")) or "",
            resources=frozenset(resources),
            defaults=defaults,
            downloads=allows_downloads(data.get("allowDownloads")),
        )


def addon_base(url: str) -> httpx.URL:
    """An add-on's base URL from its base or manifest URL: a ``/manifest.json`` ending the
    path is dropped; the query (part of the add-on's link, sent with every request) is
    kept."""
    base = httpx.URL(url.strip())
    path, mark, query = base.raw_path.partition(b"?")
    if path.endswith(b"/manifest.json"):
        path = path[: -len(b"/manifest.json")]
    return base.copy_with(raw_path=(path or b"/") + mark + query)


def sendable(value: Any) -> bool:
    """Whether a stored setting is one an add-on can be sent: text, a number, on or off (or
    nothing). A list or a table - the configuration file allows them - is not: it has no
    form as a query parameter, and went out as an empty value."""
    return value is None or isinstance(value, (str, int, float, bool))


def _setting_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return text(value) or ""


@dataclass(frozen=True)
class StreamInfo:
    url: str = field(repr=False)
    transport: str  # direct | hls | dash
    format: str | None = None
    container: str | None = None
    codec: str | None = None
    quality: str | None = None
    expires_at: float | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, data: Mapping[str, Any]) -> StreamInfo:
        if "url" not in data:
            wrapped = data.get("stream")
            streams = data.get("streams")
            if isinstance(wrapped, dict):
                data = wrapped
            elif isinstance(streams, list) and streams and isinstance(streams[0], dict):
                data = streams[0]
        url = text(data.get("url"))
        if not url or not url.lower().startswith(("http://", "https://")):
            raise AddonError("invalid", "stream response without an http(s) URL")
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, ValueError, TypeError):
            raise AddonError("invalid", "stream response with a malformed URL") from None
        if not parsed.host:
            raise AddonError("invalid", "stream response with a malformed URL")
        manifest = (text(data.get("manifest")) or "").lower()
        transport = TRANSPORTS.get(manifest)
        if transport is None:
            path = parsed.path.lower()
            transport = (
                "hls" if path.endswith(".m3u8") else "dash" if path.endswith(".mpd") else "direct"
            )
        headers = {}
        raw_headers = data.get("headers")
        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                name, val = text(key), text(value)
                if name and val and name.lower() not in _FORBIDDEN_HEADERS:
                    headers[name] = val
        expires = number(data.get("expiresAt"))
        if expires is not None and expires > 1e11:  # milliseconds
            expires /= 1000
        return cls(
            url=url,
            transport=transport,
            format=(text(data.get("format")) or "").lower() or None,
            container=(text(data.get("container")) or "").lower() or None,
            codec=(text(data.get("codec")) or "").lower() or None,
            quality=text(data.get("quality")),
            expires_at=expires,
            headers=headers,
        )


_FORBIDDEN_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "range"}


@dataclass(frozen=True)
class Availability:
    available: bool | None  # None: the add-on cannot tell
    track_id: str | None = None  # the add-on's ID for the recording, if it found one
    preparing: bool = False


@dataclass(frozen=True)
class Wanted:
    """The recording a placeholder stands for. ``version``: "clean" or "explicit" when the
    catalog says which version it is."""

    isrc: str | None
    title: str
    artist: str
    duration_ms: int
    version: str | None = None


@dataclass(frozen=True)
class Found:
    """A track an add-on named for a recording: the item of its ``/resolve``, or one of
    the tracks of its search. ``duration_ms``: None (or 0) when it sent no length."""

    id: str | None
    title: str | None
    artist: str | None
    duration_ms: float | None
    isrc: str | None
    # What a search's track says of its version besides its title (``best_match``): its
    # explicit flag (None: none sent), and its album's title.
    explicit: bool | None = None
    album: str | None = None

    @classmethod
    def resolved(cls, item: Mapping[str, Any]) -> Found:
        """A ``/resolve`` item: ``id``, ``title``, ``artist``, ``durationMs`` (or
        ``duration`` in seconds), ``isrc``."""
        return cls(
            text(item.get("id")),
            text(item.get("title")),
            text(item.get("artist")),
            _length(item),
            text(item.get("isrc")),
        )

    @classmethod
    def searched(cls, item: Mapping[str, Any]) -> Found:
        """A track of a search's answer, read as the catalog of an add-on reads one:
        ``title`` or ``name``; ``artist`` a name (or an object with one, or a list). A
        title or an artist longer than ``MAX_WORDS`` is none (the track is no match). Also
        its explicit flag (``explicit`` or ``isExplicit``: on or off, or words such as
        "true", "explicit", "false", "clean") and its ``album`` (a title, or an object
        with one)."""
        title = _bounded(text(item.get("title")) or text(item.get("name")))
        album = item.get("album")
        if isinstance(album, Mapping):
            album = text(album.get("title")) or text(album.get("name"))
        return cls(
            text(item.get("id")), title, _bounded(_credit(item.get("artist"))), _length(item),
            text(item.get("isrc")), _explicit(item), _bounded(text(album)),
        )  # fmt: skip


def mismatches(found: Found, wanted: Wanted) -> list[str]:
    """What tells ``found`` from the wanted recording - its "title", "artist", "length", or
    "ISRC (no length sent)" -; none: it is that recording. Title, artist and length (within
    3 s) must match, and the version: a title's version marker on one side only (live,
    remixed, ...) is other audio, and so is a clean edit - a clean track, by its title's
    "(Clean)" or the catalog's flag, needs a track marked "(Clean)", and a track marked so
    needs a clean track -, unless ``found`` carries the wanted ISRC, which settles clean or
    explicit (not the title's other markers). A track without a length will do only with
    the wanted ISRC (some add-ons send no length)."""
    want = _isrc(wanted.isrc)  # a junk ISRC ("-") matches nothing, not a missing one
    recording = len(want) == 12 and _isrc(found.isrc) == want
    duration = found.duration_ms
    # Without a length to compare, only the wanted recording itself will do.
    length = close_duration(int(duration), wanted.duration_ms) if duration else recording
    version = recording or _same_version(found.title, wanted)
    return [
        what
        for what, same in (
            ("title", same_title(found.title, wanted.title) and version),
            ("artist", same_artist(found.artist, wanted.artist)),
            ("length" if duration else "ISRC (no length sent)", length),
        )
        if not same
    ]


def best_match(tracks: list[Found], wanted: Wanted) -> tuple[Found | None, list[str]]:
    """The track of ``tracks`` that is the wanted recording (``mismatches``), and - when
    none is - what tells the closest of them from it (the fewest differences, the first of
    those). Of several that are, the best: the one with the wanted ISRC; then by the
    version - the explicit one, unless the wanted track is clean (by its title or the
    catalog's flag), then the clean one: by the track's explicit flag, then by its
    album's title ("(Clean)", "(Edited)", "(Explicit)"), each the same version first,
    unmarked next, the other version last; then one that carries no other ISRC, one whose
    title marks the version, one with a length, the closest length, the first."""
    want = _isrc(wanted.isrc)
    side = "clean" if (version_marker(wanted.title) or wanted.version) == "clean" else "explicit"

    def against(said: str | None) -> int:
        """0: the wanted version; 1: not said; 2: the other one."""
        return 1 if said is None else 0 if said == side else 2

    best: tuple[tuple[bool, int, int, bool, int, bool, float, int], Found] | None = None
    closest: list[str] = []
    for index, found in enumerate(tracks):
        differs = mismatches(found, wanted)
        if differs:
            if not closest or len(differs) < len(closest):
                closest = differs
            continue
        duration = found.duration_ms
        isrc = _isrc(found.isrc)
        flag = found.explicit
        rank = (
            not (len(want) == 12 and isrc == want),
            against(None if flag is None else "explicit" if flag else "clean"),
            against(album_version(found.album)),
            len(want) == 12 and len(isrc) == 12 and isrc != want,  # another recording's?
            against(version_marker(found.title)),
            not duration,
            abs(duration - wanted.duration_ms) if duration else 0.0,
            index,
        )
        if best is None or rank < best[0]:
            best = (rank, found)
    return (best[1], []) if best is not None else (None, closest)


def album_version(album: str | None) -> str | None:
    """ "clean" or "explicit" as an album's title marks it: the markers of track titles
    ("(Clean)", "[Explicit Version]", "- Clean Edit"), and "(Edited)" as clean."""
    return version_marker(album) or ("clean" if _EDITED.search(album or "") else None)


def _explicit(item: Mapping[str, Any]) -> bool | None:
    """A track's explicit flag - ``explicit``, else ``isExplicit`` -: on or off, 1 or 0,
    or a word ("true", "explicit", "false", "clean"); None when it sends none."""
    for key in ("explicit", "isExplicit"):
        value = item.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if (
            isinstance(value, str)
            and (said := _EXPLICIT_WORDS.get(value.strip().lower())) is not None
        ):
            return said
    return None


def _rejected(differs: list[str]) -> str:
    """ "its title differs", "its title and length differ", "its title, artist and length
    differ"."""
    named = " and ".join([", ".join(differs[:-1]), differs[-1]] if len(differs) > 1
                         else differs)  # fmt: skip
    return f"its {named} {'differs' if len(differs) == 1 else 'differ'}"


@dataclass
class _Reading:
    """A manifest's fetch under way: ``done`` once it ended, ``error`` when it failed."""

    done: anyio.Event = field(default_factory=anyio.Event)
    error: AddonError | None = None
    waits: pacing.Waits | None = None  # what its request waits at the add-on's limits


class Addon:
    def __init__(
        self,
        base_url: str,
        settings: Mapping[str, Any] | None,
        http: httpx.AsyncClient,
        pace: AddonPace | None = None,
        paces: Paces | None = None,
    ) -> None:
        self.base = addon_base(base_url)
        # (A list or a table is not sent: the add-on's own default applies.)
        self.settings = {k: _setting_value(v) for k, v in (settings or {}).items() if sendable(v)}
        self.http = http
        # Its origin's limits: every request below takes its turn there first - the
        # wait is part of the caller's own time (its budget's scope cancels it). A request
        # redirected to another origin takes its turn at that origin's (``paces``).
        self.pace = pace
        self.paces = paces
        self._manifest: Manifest | None = None
        self._reading: _Reading | None = None  # the manifest's fetch under way

    def _url(self, resource: str, params: Mapping[str, str] | None = None) -> httpx.URL:
        defaults = self._manifest.defaults if self._manifest else {}
        query = self.base.params.merge(defaults).merge(self.settings).merge(params or {})
        path = self.base.raw_path.split(b"?", 1)[0].rstrip(b"/") + b"/" + resource.encode()
        if query:
            path += b"?" + str(query).encode()
        return self.base.copy_with(raw_path=path)

    def _pace_at(self, url: httpx.URL) -> AddonPace | None:
        """The limits of the origin ``url`` is at: the add-on's own, or - a redirect's hop
        elsewhere - that origin's."""
        pace = self.paces.of(str(url)) if self.paces is not None else None
        return pace or self.pace

    async def _json(self, resource: str, params: Mapping[str, str] | None = None) -> Any:
        asked: list[AddonPace] = []  # the limits of the origin each hop went to

        async def turn(url: httpx.URL) -> None:
            pace = self._pace_at(url)
            if pace is not None:
                await pace.request()
                asked.append(pace)

        def limited(named: float | None = None) -> None:
            """ "Too many requests": the origin that said so is left alone from here on -
            whoever asked (a lookup, a check, a preparation request in the background)."""
            if asked:
                asked[-1].limited(named)

        try:
            async with pacing.get(
                self.http,
                self._url(resource, params),
                turn=turn,
                headers={"accept": "application/json"},
            ) as response:
                if response.status_code == 429:
                    retry = pacing.retry_after(response.headers.get("retry-after"))
                    limited(retry)
                    raise AddonError("rate_limited", "HTTP 429", retry)
                if response.status_code == 404:
                    raise AddonError("unavailable", "not found (HTTP 404)")
                if response.status_code == 410:
                    raise AddonError("expired", "gone (HTTP 410)")
                if response.status_code in (401, 403):
                    raise AddonError("unavailable", f"refused (HTTP {response.status_code})")
                if response.status_code >= 500:
                    raise AddonError("failed", f"add-on error (HTTP {response.status_code})")
                if response.status_code != 200:
                    raise AddonError("invalid", f"unexpected HTTP {response.status_code}")
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_JSON_BYTES:
                        raise AddonError("broken", "response too large")
        except pacing.Blocked:  # left alone after a rate limit: nothing is sent
            raise AddonError("cooling", pacing.BLOCKED) from None
        except httpx.ConnectTimeout as exc:  # no connection: an error, not a slow answer
            raise AddonError("failed", type(exc).__name__) from exc
        except httpx.TimeoutException as exc:
            raise AddonError("timeout", type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            if denied(exc):
                raise AddonError("denied", "destination not allowed by the network policy") from exc
            raise AddonError("failed", type(exc).__name__) from exc
        try:
            data = json.loads(body)
        except (ValueError, RecursionError) as exc:  # (nested too deeply: no JSON of ours)
            raise AddonError("broken", "response is not JSON") from exc
        if isinstance(data, dict):
            error = text(data.get("error")) or ""
            if "ratelimit" in error.replace(" ", "").replace("_", "").lower():
                limited()
                raise AddonError("rate_limited", "add-on reported a rate limit")
        return data

    async def manifest(self) -> Manifest:
        """The add-on's manifest, read once: requests that need it while it is being read
        wait for that one fetch - and get its failure when it fails (``shared``); when
        the request that read it is cut short, one of them reads it."""
        while self._manifest is None:
            reading = self._reading
            if reading is not None:
                await self._read_by(reading)
                continue
            reading = self._reading = _Reading()
            try:
                with pacing.watched() as reading.waits:
                    self._manifest = await self._read_manifest()
            except AddonError as exc:
                reading.error = exc
                raise
            except Exception as exc:  # any other failure is shared too (not fetched by each)
                reason = f"unreadable manifest ({type(exc).__name__})"
                reading.error = AddonError("broken", reason)
                raise reading.error from None
            finally:  # (cut short: no error to share - the next one waiting reads it)
                self._reading = None
                reading.done.set()
        return self._manifest

    async def _read_by(self, reading: _Reading) -> None:
        """Wait for another request's read of the manifest. What that request waits at the
        add-on's request limit, this one waits there too (its time running out meanwhile is
        no slow add-on); its failure is this one's (``shared``)."""
        mine = pacing.watching()
        joined = reading.waits.joined() if reading.waits is not None else None
        try:
            await reading.done.wait()
        finally:
            if mine is not None and reading.waits is not None and joined is not None:
                mine.shared(reading.waits, joined)
        if reading.error is not None:
            error = reading.error
            raise AddonError(error.kind, error.reason, error.retry_after, shared=True)

    async def _read_manifest(self) -> Manifest:
        data = await self._json("manifest.json")
        if not isinstance(data, dict):
            raise AddonError("broken", "manifest is not an object")
        try:
            return Manifest.parse(data)
        except Exception as exc:
            raise AddonError("broken", f"unreadable manifest ({type(exc).__name__})") from None

    async def resolve_isrc(self, isrc: str) -> str | None:
        manifest = await self.manifest()
        if "isrc" not in manifest.resources:
            return None
        try:
            data = await self._json("resolve-isrc", {"isrc": isrc.upper()})
        except AddonError as exc:
            if exc.kind == "unavailable" and "404" in exc.reason:
                return None
            raise
        if not isinstance(data, dict):
            return None
        return text(data.get("trackId")) or text(data.get("id"))

    async def resolve_recording(self, wanted: Wanted, notes: list[str] | None = None) -> str | None:
        """``/resolve`` fallback: its item is accepted only when it is the wanted recording
        (``mismatches``: title, artist, length, version; an item without a length only with
        the wanted ISRC). A rejected match is noted in ``notes``, for the log."""
        manifest = await self.manifest()
        if "resolve" not in manifest.resources:
            return None
        params = {
            "title": wanted.title,
            "artist": wanted.artist,
            "durationMs": str(wanted.duration_ms),
        }
        if wanted.isrc:
            params["isrc"] = wanted.isrc
        try:
            data = await self._json("resolve", params)
        except AddonError as exc:
            if exc.kind == "unavailable":
                return None
            raise
        item = data.get("item") if isinstance(data, dict) else None
        if not isinstance(item, dict):
            return None
        found = Found.resolved(item)
        differs = mismatches(found, wanted)
        if differs:
            if notes is not None:
                notes.append(f"a match was rejected: {_rejected(differs)}")
            return None
        return found.id

    async def search_recording(self, wanted: Wanted, notes: list[str] | None = None) -> str | None:
        """The wanted recording found through the add-on's search, ``GET /search?q=<artist>
        <title>``: of the tracks it answers with (its first ``SEARCHED``), the best that is
        the wanted recording by the rules of ``/resolve`` (``best_match``); none when
        none is. When tracks were there but none was it, ``notes`` say why the closest was
        not (for the log). Errors as ``/resolve``'s: "not found" (HTTP 404) and a refusal
        (401, 403) are None - the add-on lacks the recording; the others are raised."""
        manifest = await self.manifest()
        if "search" not in manifest.resources:
            return None
        term = f"{wanted.artist} {wanted.title}".strip()
        try:
            data = await self._json("search", {"q": term})
        except AddonError as exc:
            if exc.kind == "unavailable":
                return None
            raise
        listed = data.get("tracks") if isinstance(data, dict) else None
        tracks = [
            found
            for item in (listed[:SEARCHED] if isinstance(listed, list) else [])
            if isinstance(item, dict) and (found := Found.searched(item)).id is not None
        ]
        best, closest = best_match(tracks, wanted)
        if best is not None:
            return best.id
        if closest and notes is not None:
            of = "the search's one track" if len(tracks) == 1 else (
                f"the closest of {len(tracks)} tracks searched")  # fmt: skip
            notes.append(f"a match was rejected: {_rejected(closest)} ({of})")
        return None

    async def find(self, wanted: Wanted, notes: list[str] | None = None) -> str | None:
        """The add-on's track ID for the wanted recording: by its ISRC lookup when it has
        one and the recording an ISRC; then by ``/resolve`` - or, at an add-on without
        ``/resolve``, by its search. None: not found (or no way to look)."""
        track_id = await self.resolve_isrc(wanted.isrc) if wanted.isrc else None
        if track_id is None:
            if "resolve" in (await self.manifest()).resources:
                track_id = await self.resolve_recording(wanted, notes)
            else:
                track_id = await self.search_recording(wanted, notes)
        return track_id

    async def availability(self, wanted: Wanted, *, prepare: bool = False) -> Availability | None:
        """The add-on's answer to "can you deliver this now?", or None if it has no such
        check (the ``availability`` extension)."""
        manifest = await self.manifest()
        if "availability" not in manifest.resources:
            return None
        params = {
            "title": wanted.title,
            "artist": wanted.artist,
            "durationMs": str(wanted.duration_ms),
        }
        if wanted.isrc:
            params["isrc"] = wanted.isrc
        if prepare:
            params["prepare"] = "true"
        try:
            data = await self._json("availability", params)
        except AddonError as exc:
            if exc.kind == "unavailable":
                return Availability(False)
            raise
        if not isinstance(data, dict):
            return Availability(None)
        value = data.get("available")
        available = value if isinstance(value, bool) else None
        return Availability(available, text(data.get("id")), data.get("preparing") is True)

    @property
    def known(self) -> Manifest | None:
        """The manifest as read before (``manifest``); None while it has not been."""
        return self._manifest

    async def reread(self) -> Manifest:
        """The manifest read again now (a check of the add-on): one request, past the one
        read before, which it replaces."""
        self._manifest = await self._read_manifest()
        return self._manifest

    async def has_catalog(self) -> bool:
        """Whether the add-on declares a catalog: its search and its albums and
        artists (the resources ``search`` and ``catalog``)."""
        resources = (await self.manifest()).resources
        return all(resource in resources for resource in CATALOG)

    async def search(self, term: str) -> Any:
        """The add-on's answer to ``GET /search?q=`` as it sent it (``catalog.addon`` reads
        it): tracks, albums and artists for ``term``."""
        return await self._json("search", {"q": term})

    async def album(self, album_id: str) -> Any:
        """Its answer to ``GET /album/{id}``: the album with its tracks."""
        return await self._json("album/" + _segment(album_id))

    async def artist(self, artist_id: str) -> Any:
        """Its answer to ``GET /artist/{id}``: the artist with albums and top tracks."""
        return await self._json("artist/" + _segment(artist_id))

    async def stream(self, track_id: str) -> StreamInfo:
        manifest = await self.manifest()
        if "stream" not in manifest.resources:
            raise AddonError("unavailable", "add-on does not stream")
        data = await self._json("stream/" + _segment(track_id))
        if not isinstance(data, dict):
            raise AddonError("broken", "stream response is not an object")
        try:
            return StreamInfo.parse(data)
        except AddonError:
            raise
        except Exception as exc:  # anything else in a hostile or broken answer
            raise AddonError(
                "broken", f"unreadable stream response ({type(exc).__name__})"
            ) from None


def _segment(own: str) -> str:
    """An add-on's ID as one segment of a request's path. "." and ".." are none: sent, they
    would name another address of the add-on."""
    if own in ("", ".", ".."):
        raise AddonError("invalid", "an ID that cannot be asked for")
    return quote(own, safe="")


def _isrc(value: str | None) -> str:
    return "".join(ch for ch in value or "" if ch.isalnum()).upper()


def _length(item: Mapping[str, Any]) -> float | None:
    """A track's length in milliseconds: ``durationMs``, else ``duration`` in seconds."""
    duration = number(item.get("durationMs"))
    if duration is None and (seconds := number(item.get("duration"))) is not None:
        duration = seconds * 1000
    return duration


def _credit(value: Any) -> str | None:
    """A track's artist: a name, an object's ``name`` (or ``title``), or several of
    either."""
    if isinstance(value, list):
        names = (_credit(item) for item in value[:20] if not isinstance(item, list))
        return ", ".join(name for name in names if name) or None
    if isinstance(value, Mapping):
        value = text(value.get("name")) or text(value.get("title"))
    return text(value)


def _bounded(words: str | None) -> str | None:
    return words if words is None or len(words) <= MAX_WORDS else None


def _same_version(title: str | None, wanted: Wanted) -> bool:
    """A clean edit is other audio: a clean track (its title or the catalog's flag)
    and an item marked "(Clean)" go only together. An explicit or plain item is otherwise
    the track's version (plain titles are the usual, explicit, version)."""
    item = version_marker(title)
    want = version_marker(wanted.title) or wanted.version
    return (item == "clean") == (want == "clean")
