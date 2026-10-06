"""The catalog of an add-on: ``[catalog] kind = "addon"``.

The add-on protocol Shijhon speaks for audio also has catalog requests - a search for
tracks, albums and artists, an album with its tracks, an artist with albums and top tracks.
This catalog takes them from one of the installation's add-ons, named in ``[catalog]
addon``, the way audio is taken from add-ons: through the add-on client
(``delivery.addon``), so with that add-on's address, settings and network reach, and at its
request limit - a catalog request counts there like a lookup, and the song being
played goes first.

**The requests** (``GET``, JSON; declared in the manifest as the resources ``search`` and
``catalog`` - an add-on without both is refused, with the reason):

- ``/search?q=`` - ``{"tracks": [...], "albums": [...], "artists": [...]}`` (anything else
  in it, such as playlists, is not read);
- ``/album/{id}`` - the album's fields and ``"tracks"``;
- ``/artist/{id}`` - the artist's fields, ``"albums"`` and ``"topTracks"`` (or ``"tracks"``).

**The fields read**, each optional unless said, with the other names add-ons use for them:

- a track: ``id`` and ``title`` (or ``name``) - needed; a length, ``duration`` in seconds
  (or ``durationMs``) - needed: a track without one is left out, since a placeholder is a
  file of the song's length; ``artist`` (a name), ``album`` (a name), ``isrc``,
  ``artworkURL``;
- an album: ``id`` (in a list) and ``title`` (or ``name``) - needed; ``artist``, ``year`` or
  ``releaseDate``, ``trackCount`` (or ``totalTracks``), ``artworkURL`` (or ``cover``);
- an artist: ``id`` and ``name`` (or ``title``) - needed; ``artworkURL`` (or ``cover``).

An item that lacks what is needed is left out of its list; an album whose answer lacks a
track that way - or lists fewer tracks than it says it has, or none - is shown with what
it has and marked incomplete (never filled into an owned album). An album or artist answer
that names another ID than the one asked for is refused ("invalid"); one that names none
is the item asked for.

**What the protocol does not say, and what is made of it:**

- *Track and disc numbers:* none are sent. An album's tracks are numbered in the order of
  its answer, all on disc 1 (the release says so, ``numbered`` off: it is filled into an
  owned album only when the owned files are numbered the same way, else it goes to
  review); a song of a search or of an artist's top tracks has no number.
- *Release kinds:* none are sent. Every release is an album, except one whose title ends in
  "- Single" or "- EP".
- *Artists* are names on tracks and albums, items with an ID only in a search's artist list
  and on an artist's page. So an artist credited on a track or an album is an item *by its
  name* (``n<name>``; a credit is split on "feat." alone, as everywhere), opened by finding
  that name: among the artists the add-on's answers named since the start, else with one
  search - an artist page reached through a credit costs that search once, then the one
  artist request. The artists a search lists are the add-on's own items (``i<id>``).
- *Lengths* are whole seconds.
- *ISRCs* are optional: a song without one is found at the other add-ons by title, artist
  and length. The protocol's ISRC lookup names a track ID alone, without its album, so
  ``songs_by_isrc`` finds nothing: owned albums are matched by their titles.
- *One song:* the protocol has no request for a single track. A song is what the last
  answer that showed it said (the most recent ten thousand are remembered); after a
  restart a song's ID is not found until an answer shows it again.
- *A song's album:* tracks name their album by its title only. A song of a search or of an
  artist's top tracks therefore has no album reference - unless an album answer since the
  start showed that song on an album of that title. When such a song is added to the
  library, its album is looked for (``album_of``): a search for the album's title and the
  artist, then the albums of that title until one lists the song (three at most), within
  the catalog's timeout.
- *Top songs:* the artist answer's ``topTracks``, in its order.
- *Paging:* the protocol has none. A search is one request, cut to the count asked for; an
  artist's releases are what the artist answer lists.
- *Clean and explicit versions*: none are flagged, so none are reported; twins by
  title, artist and length are still one song.

**IDs.** The catalog's key is made from the add-on's configured name
(``delivery.addon.catalog_key``), so the items of two add-ons never meet, nor an
add-on's and another catalog's. The ID of an item the add-on listed is a tag of the
service the add-on is (a digest of its manifest's ``id``, which it must have:
``delivery.addon.service_tag``), then the add-on's own ID,
with every character that is no letter or digit written as a dot and two hexadecimal
digits (``delivery.addon.item_id``). Naming another add-on, or renaming this one, is a
switch to another catalog, with what that always means: what is in the
library stays and plays - at this add-on by the IDs it gave, while it keeps its name and is
the same service; elsewhere by ISRC or title, artist and length -, and the new catalog
serves searches, artist pages and the albums not yet filled. An add-on pointed at another
service under its old name keeps the key, but not the items: those of the service before
are not found (their tag is another), and nothing of them is asked of the new one.

**Artwork** addresses are whatever hosts the add-on names. They are fetched through the
public-addresses-only network policy, up to 10 MiB, by a client of their own (never with
the add-on's reach: an add-on on a private network cannot make Shijhon fetch from there),
and the core serves only raster images (``artwork.raster_type``). An address is this
catalog's own when the add-on named it as artwork: the catalog marks the addresses it
hands out (a short digest in the fragment, which is never sent), so that holds across
restarts without a list. A cover request that goes to an add-on's own (public) address -
at once or by a redirect - takes a turn at its request limit, as background work, and its
"too many requests" leaves that add-on alone.

**What is read of an answer is bounded:** titles, names and credits to 500 characters,
lists to their first 100 items (an album's tracks and an artist's albums: 500), artwork
addresses to 1,000 characters; whatever else a broken answer holds makes it "invalid".

**Failures** are catalog errors like any adapter's - the add-on not enabled, cooling down
or left alone after "too many requests", slow, down, or without a catalog: views then
answer from the library.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import anyio
import httpx
from async_lru import alru_cache
from pydantic import BaseModel, Field

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
)
from shijhon.catalog.plugin import Adapter, Context, Problem, Words, denied
from shijhon.delivery import pacing
from shijhon.delivery.addon import (
    CATALOG,
    MAX_ITEM_ID,
    SERVICE_TAG,
    Addon,
    AddonError,
    Manifest,
    catalog_key,
    item_id,
    number,
    own_id,
    service_tag,
    tagged,
    text,
    untagged,
)
from shijhon.delivery.netpolicy import Reach, Resolver, allowed, system_resolver
from shijhon.matching.normalize import credit_names, featured_names, fold, same_artist

if TYPE_CHECKING:
    from shijhon.delivery.sources import Source, SourceRegistry

KIND = "addon"
LABEL = "An add-on's catalog"
MAX_ARTWORK_BYTES = 10 * 1024 * 1024
MAX_ADDRESS = 1000  # characters of an artwork address
MAX_TEXT = 500  # characters of a title, a name or a credit (the rest is cut)
# Items read of a list at most: a search's tracks, albums and artists, an artist's top
# tracks; an album's tracks, an artist's albums.
MAX_LISTED = 100
MAX_TRACKS = 500
MAX_ALBUMS = 500
MAX_LENGTH_MS = 24 * 3600 * 1000  # a longer "length" is no song's
SONGS = 10_000  # songs of recent answers remembered (the protocol has no request for one)
ARTISTS = 5_000  # artists' names remembered with their IDs
ANSWERS = 256  # search and artist answers kept
MIN_KEPT = 60.0  # ... for at least this long: an artist page is three questions, one request
CANDIDATES = 3  # albums opened at most to find a song's album
NO_CATALOG = "the add-on has no catalog (its manifest declares no search and catalog resources)"
NO_IDENTITY = "the add-on's manifest has no id: its catalog's items could not be told apart"
NOT_ENABLED = "the add-on the catalog names is not among the enabled add-ons"
NOT_PUBLIC = "artwork: an address the network policy does not allow"
OTHER_SERVICE = "an item of the service the add-on was before: not found at the one it is now"
_ISRC = re.compile(r"[A-Z0-9]{12}")
_DATE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
_KIND = re.compile(r"\s+-\s+(single|ep)\s*$", re.IGNORECASE)
_ARTIST_ITEM = "i"  # an artist by the add-on's own ID
_ARTIST_NAME = "n"  # ... by its name, as a credit gives it


# --- reading the add-on's answers -----------------------------------------------------------


def _first(item: Mapping[str, Any], *names: str) -> Any:
    """The first of the item's fields that is set (add-ons name some fields differently)."""
    for name in names:
        value = item.get(name)
        if value is not None and value != "":
            return value
    return None


def _words(value: Any) -> str | None:
    """A title, a name or a credit: text, at most ``MAX_TEXT`` characters of it (what is
    read is compared and split with patterns: never megabytes of it)."""
    words = (text(value) or "")[:MAX_TEXT]
    # (A lone surrogate is no text an answer can be written with: replaced.)
    return words.encode("utf-8", "replace").decode("utf-8").strip() or None


def _credit(value: Any) -> str:
    """A credit as text: a name, or the name of an object, or several of either."""
    if isinstance(value, list):
        names = (_credit(item) for item in value[:20] if not isinstance(item, list))
        return ", ".join(name for name in names if name)[:MAX_TEXT].strip()
    if isinstance(value, Mapping):
        value = _first(value, "name", "title")
    return _words(value) or ""


def _date(item: Mapping[str, Any]) -> str | None:
    """``releaseDate`` as far as it is a date (a time after it goes), else ``year``."""
    for value in (text(item.get("releaseDate")), text(item.get("year"))):
        match = _DATE.match(value or "")
        if match is not None and match.group(1) != "0000":
            return "-".join(part for part in match.groups() if part)
    return None


def _length_ms(item: Mapping[str, Any]) -> int | None:
    length = number(item.get("durationMs"))
    if length is None and (seconds := number(item.get("duration"))) is not None:
        length = seconds * 1000
    if length is None or not 0 < length <= MAX_LENGTH_MS:
        return None
    return max(1, round(length))


def _isrc(value: Any) -> str | None:
    code = "".join(ch for ch in text(value) or "" if ch.isalnum()).upper()
    return code if _ISRC.fullmatch(code) else None


def _count(value: Any) -> int | None:
    count = number(value)
    return int(count) if count is not None and 0 <= count <= 100_000 else None


def _mark(key: str, address: str) -> str:
    return "sh=" + hashlib.sha256(f"{key} {address}".encode()).hexdigest()[:12]


def artwork_address(key: str, *values: Any) -> str | None:
    """The first of ``values`` that is an http(s) address, as the catalog ``key`` hands
    it out: without braces (an artwork template's placeholders), and marked as an address
    this catalog named (``unmarked``)."""
    for value in values:
        address = (text(value) or "").split("#", 1)[0]
        if not address or len(address) > MAX_ADDRESS:
            continue
        if any(ch.isspace() or not ch.isprintable() for ch in address):
            continue
        address = address.replace("{", "%7B").replace("}", "%7D")
        try:
            parsed = httpx.URL(address)
        except (httpx.InvalidURL, ValueError, TypeError):
            continue
        if parsed.scheme not in ("http", "https") or not parsed.host or parsed.userinfo:
            continue
        return f"{address}#{_mark(key, address)}"
    return None


def unmarked(key: str, url: str) -> str | None:
    """The address to fetch for an artwork address the catalog ``key`` handed out; None
    for any other address."""
    address, _, mark = url.rpartition("#")
    return address if address and mark == _mark(key, address) else None


@dataclass(frozen=True)
class Space:
    """Whose items an answer's are: the catalog's key (the add-on's name) and the service
    the add-on is (its manifest's ``id``)."""

    key: str
    service: str

    def ref(self, own: Any, kind: str = "") -> CatalogRef | None:
        """The item of the add-on's own ID ``own`` (``kind``: "i" for an artist)."""
        ident = tagged(self.service, own)
        if ident is None or len(kind) + len(ident) > MAX_ITEM_ID:
            return None
        return CatalogRef(self.key, kind + ident)

    def own(self, item: str) -> str | None:
        """The add-on's own ID of an item of this service; None for any other."""
        return untagged(self.service, item)


def _by_name(name: str) -> str | None:
    """An artist item's ID for a credited name; a credit too long to carry: its first
    name."""
    for candidate in (name, *credit_names(name)[:1]):
        ident = item_id(candidate)
        if ident is not None and len(ident) < MAX_ITEM_ID:
            return _ARTIST_NAME + ident
    return None


def credited(key: str, credit: str) -> tuple[CatalogRef, ...]:
    """The artist items of a credit: one for each name around "feat." (as entries pair
    them), else one for the whole credit; none when a name cannot be carried."""
    if not credit:
        return ()
    refs = []
    for name in featured_names(credit) or [credit]:
        ident = _by_name(name)
        if ident is None:
            return ()
        refs.append(CatalogRef(key, ident))
    return tuple(refs)


def read_track(
    space: Space, item: Any, *, album: CatalogRelease | None = None, position: int = 0
) -> CatalogTrack | None:
    """A track of an answer; None when it lacks an ID, a title or a length. ``album``: the
    album whose answer lists it, at ``position``."""
    if not isinstance(item, Mapping):
        return None
    key = space.key
    ref = space.ref(item.get("id"))
    title = _words(_first(item, "title", "name"))
    length = _length_ms(item)
    if ref is None or title is None or length is None:
        return None
    credit = _credit(item.get("artist")) or (album.artist if album is not None else "")
    own = artwork_address(key, item.get("artworkURL"), item.get("cover"))
    return CatalogTrack(
        ref=ref,
        title=title,
        artist=credit,
        duration_ms=length,
        number=position,
        isrc=_isrc(item.get("isrc")),
        album=album.ref if album is not None else None,
        artist_refs=credited(key, credit),
        album_title=album.title if album is not None else _credit(item.get("album")) or None,
        release_date=album.release_date if album is not None else None,
        artwork_template=own or (album.artwork_template if album is not None else None),
    )


def read_release(
    space: Space, item: Any, *, own: str | None = None, artist: str = ""
) -> CatalogRelease | None:
    """An album of an answer, without tracks; None when it lacks an ID or a title.
    ``own``: the add-on's ID it was asked for with; ``artist``: whose list it is in (its
    credit when it names none)."""
    if not isinstance(item, Mapping):
        return None
    key = space.key
    ref = space.ref(own if own is not None else item.get("id"))
    title = _words(_first(item, "title", "name"))
    if ref is None or title is None:
        return None
    credit = _credit(item.get("artist")) or artist
    kind = _KIND.search(title)
    return CatalogRelease(
        ref=ref,
        title=title,
        artist=credit,
        kind=ReleaseKind(kind.group(1).lower()) if kind else ReleaseKind.ALBUM,
        release_date=_date(item),
        track_count=_count(_first(item, "trackCount", "totalTracks")),
        artist_refs=credited(key, credit),
        artwork_template=artwork_address(key, item.get("artworkURL"), item.get("cover")),
        numbered=False,  # (its tracks are counted in the order of its answer)
    )


def _other(data: Any, own: str) -> bool:
    """The answer names another item than the one asked for (``own``: the add-on's ID it
    was asked with): not read as that one. An answer without an ID is the item asked for."""
    named = data.get("id") if isinstance(data, Mapping) else None
    if named is None or named == "":
        return False
    return (named if isinstance(named, str) else text(named)) != own


def read_album(space: Space, own: str, data: Any) -> CatalogRelease:
    """The answer to the request for the album ``own`` (the add-on's ID): the release with
    its tracks, numbered in the answer's order on disc 1; incomplete when a track could not
    be read (or it lists more than ``MAX_TRACKS``)."""
    release = read_release(space, data, own=own)
    if release is None:
        raise CatalogError("not_found", "the add-on has no such album")
    if _other(data, own):
        raise CatalogError("invalid", "the add-on answered with another album")
    listed = data.get("tracks")
    listed = listed if isinstance(listed, list) else []
    tracks: dict[str, CatalogTrack] = {}
    for position, item in enumerate(listed[:MAX_TRACKS], start=1):
        track = _item(lambda item=item, position=position: read_track(  # type: ignore[misc]
            space, item, album=release, position=position
        ))  # fmt: skip
        if track is not None:
            tracks.setdefault(track.ref.id, track)
    count = release.track_count if release.track_count is not None else len(listed)
    return replace(
        release,
        tracks=tuple(tracks.values()),
        track_count=count,
        # Every track it lists and says it has must be there: one left out, a list shorter
        # than its count, or no list at all, and it is never filled into an owned album.
        incomplete=not tracks or len(tracks) < max(len(listed), count),
    )


def read_artist(space: Space, item: Any, *, own: str | None = None) -> CatalogArtist | None:
    """An artist of an answer; None without an ID or a name. ``own``: the add-on's ID it
    was asked for with."""
    if not isinstance(item, Mapping):
        return None
    if own is not None and _other(item, own):
        raise CatalogError("invalid", "the add-on answered with another artist")
    ref = space.ref(own if own is not None else item.get("id"), _ARTIST_ITEM)
    name = _words(_first(item, "name", "title"))
    if ref is None or name is None:
        return None
    return CatalogArtist(
        ref, name, artwork_address(space.key, item.get("artworkURL"), item.get("cover"))
    )


def _each[T](items: Any, read: Callable[[Any], T | None], most: int = MAX_LISTED) -> tuple[T, ...]:
    """The items of a list that can be read, each ID once, in order (of its first ``most``)."""
    found: dict[str, T] = {}
    for item in items[:most] if isinstance(items, list) else []:
        made = _item(lambda item=item: read(item))  # type: ignore[misc]
        if made is not None:
            found.setdefault(made.ref.id, made)  # type: ignore[attr-defined]
    return tuple(found.values())


def _item[T](read: Callable[[], T | None]) -> T | None:
    """One item of a list; one that cannot be read, whatever it raises, is left out."""
    try:
        return read()
    except Exception:
        return None


def _reading[T](read: Callable[[], T]) -> T:
    """What ``read`` makes of an answer; anything else that a broken or hostile answer
    raises is the catalog's "invalid", never another exception."""
    try:
        return read()
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError("invalid", f"unreadable answer ({type(exc).__name__})") from None


def _failure(exc: AddonError) -> CatalogError:
    """An add-on's failure as a catalog's (its reasons name no address)."""
    if exc.kind in ("rate_limited", "cooling"):
        return CatalogError("rate_limited", exc.reason)
    if exc.kind == "expired" or (exc.kind == "unavailable" and "404" in exc.reason):
        return CatalogError("not_found", "not in the add-on's catalog")
    if exc.kind == "unavailable":  # a refusal (HTTP 401, 403)
        return CatalogError("unauthorized", exc.reason)
    if exc.kind in ("invalid", "broken"):
        return CatalogError("invalid", exc.reason)
    return CatalogError("unavailable", exc.reason)  # no answer, no connection, not allowed


def _service(manifest: Manifest) -> str:
    """The service an add-on with a catalog is: its manifest's ``id``. Refused: an add-on
    that declares no catalog, or names no ID (its items could not be told from those of
    another service under the same name)."""
    if not all(resource in manifest.resources for resource in CATALOG):
        raise CatalogError("invalid", NO_CATALOG)
    if not manifest.id:
        raise CatalogError("invalid", NO_IDENTITY)
    return manifest.id


@dataclass(frozen=True)
class _Page:
    """An artist request's answer."""

    artist: CatalogArtist
    releases: tuple[CatalogRelease, ...]
    top: tuple[CatalogTrack, ...]


# --- the catalog --------------------------------------------------------------------------


class AddonCatalog:
    """The catalog interface over the add-on named ``name``. ``registry``: the
    installation's add-ons (None: none here - every request fails as unavailable); the
    add-on is looked up there for every request, so one added, changed or switched off in
    the dashboard is the one asked from then on. ``http``: the client covers are fetched
    with (public addresses only), closed with the catalog; ``resolver``: how a cover's
    host is looked up before a turn at the add-on's limit is taken for it."""

    region = ""
    # An add-on names any address for a cover: it is fetched by ``artwork`` alone (public
    # hosts only, paced, a raster image of a bounded size), never handed to a client.
    client_artwork = False

    def __init__(
        self,
        name: str,
        registry: SourceRegistry | None,
        http: httpx.AsyncClient,
        *,
        timeout: float = 15.0,
        ttl: float = 3600.0,
        resolver: Resolver = system_resolver,
    ) -> None:
        self.name = name
        self.key = catalog_key(name)
        self.registry = registry
        self.http = http
        self.timeout = timeout
        self.resolver = resolver
        # The songs of recent answers by ID: what ``song`` knows.
        self._songs: OrderedDict[str, CatalogTrack] = OrderedDict()
        # The add-on's own artist IDs by folded name, each with its service's tag: how a
        # credited name is opened.
        self._artists: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._service: str | None = None  # the service the add-on was when last asked
        kept = max(MIN_KEPT, ttl)
        # A search's whole answer (cut to the count asked for on the way out) and an artist
        # request's: asked once, whoever asks and for which part.
        self._found = alru_cache(maxsize=ANSWERS, ttl=kept)(self._search)
        self._page = alru_cache(maxsize=ANSWERS, ttl=kept)(self._artist_page)

    async def aclose(self) -> None:
        await self.http.aclose()

    # --- asking the add-on ------------------------------------------------------------------

    async def _source(self) -> Source:
        if self.registry is None:
            raise CatalogError("unavailable", "no add-ons here")
        for source in await self.registry.enabled():
            if source.name == self.name:
                return source
        raise CatalogError("unavailable", NOT_ENABLED)

    def _not_asked(self, source: Source) -> None:
        """Nothing is sent while the add-on cools down or is left alone after a rate
        limit."""
        assert self.registry is not None
        if source.pace is not None and source.pace.blocked > 0:
            raise CatalogError("rate_limited", pacing.BLOCKED)
        if self.registry.cooling(source.id):
            raise CatalogError("unavailable", "not asked: the add-on is cooling down")

    async def _ask[T](
        self, request: Callable[[Addon], Awaitable[T]], *, service: str | None = None
    ) -> tuple[T, Space]:
        """One request to the add-on, within the catalog's timeout - its wait at the
        add-on's request limit included, behind the song being played: as urgent as
        whoever asks says (``pacing.urgent``), else as a client's other requests. Not sent
        while the add-on cools down or is left alone after a rate limit, nor - ``service``:
        the tag of the item asked about - for an item of another service than the add-on
        is now. Returns the answer, and whose items it holds."""
        source = await self._source()
        self._not_asked(source)
        urgency = pacing.current() or pacing.Urgency(pacing.QUEUED)
        waits = pacing.Waits()
        try:
            with pacing.urgent(urgency), pacing.watched() as waits, anyio.fail_after(self.timeout):
                manifest = await source.addon.manifest()
                space = self._space(_service(manifest))
                if service is not None and service != service_tag(space.service):
                    raise CatalogError("not_found", OTHER_SERVICE)
                return await request(source.addon), space
        except TimeoutError:
            reason = waits.held() or "no answer in time"
            raise CatalogError("unavailable", reason) from None
        except AddonError as exc:
            raise _failure(exc) from None

    def _space(self, service: str) -> Space:
        """Whose items the add-on's answers hold now. When it is another service than
        before (the add-on was pointed elsewhere), nothing of the one before is kept."""
        if self._service is not None and self._service != service:
            self._songs.clear()
            self._artists.clear()
        self._service = service
        return Space(self.key, service)

    async def check(self) -> None:
        """The dashboard's "Check now": the add-on's manifest read again, and whether it
        declares a catalog."""
        source = await self._source()
        self._not_asked(source)
        urgency = pacing.current() or pacing.Urgency(pacing.CHECK)
        try:
            with pacing.urgent(urgency), anyio.fail_after(self.timeout):
                manifest = await source.addon.reread()
        except TimeoutError:
            raise CatalogError("unavailable", "no answer in time") from None
        except AddonError as exc:
            raise _failure(exc) from None
        _service(manifest)

    # --- what the answers showed --------------------------------------------------------------

    def _keep(self, tracks: Iterable[CatalogTrack]) -> None:
        """Remember the songs of an answer (a song an album answer showed keeps its album
        when a later answer names none)."""
        for track in tracks:
            known = self._songs.get(track.ref.id)
            if known is None or track.album is not None or known.album is None:
                self._songs[track.ref.id] = track
            self._songs.move_to_end(track.ref.id)
        while len(self._songs) > SONGS:
            self._songs.popitem(last=False)

    def remember(self, songs: Iterable[CatalogTrack]) -> None:
        """Songs shown from elsewhere (a saved list of top songs, after a restart): known
        like those of an answer."""
        self._keep(song for song in songs if song.ref.catalog == self.key)

    def _with_album(self, track: CatalogTrack) -> CatalogTrack:
        """A song of an answer that names its album by title only: with the album an album
        answer showed it on, when that album has this title."""
        known = self._songs.get(track.ref.id)
        if track.album is not None or known is None or known.album is None:
            return track
        if fold(known.album_title) != fold(track.album_title):
            return track
        return replace(
            track, album=known.album, number=known.number, release_date=known.release_date
        )

    def _named(self, space: Space, name: str, own: str | None) -> None:
        """An answer named this artist: the first of a name is the one a credit opens (an
        answer of the service the add-on was before - it came late - names nobody)."""
        folded = fold(name)
        tag = service_tag(space.service)
        known = self._artists.get(folded)
        if not folded or own is None or space.service != self._service:
            return
        if known is None or known[0] != tag:
            self._artists[folded] = (tag, own)
            while len(self._artists) > ARTISTS:
                self._artists.popitem(last=False)

    # --- the catalog interface ----------------------------------------------------------

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        found = await self._found(term)
        limit = max(1, limit)
        return SearchResults(found.artists[:limit], found.albums[:limit], found.songs[:limit])

    async def _search(self, term: str) -> SearchResults:
        data, space = await self._ask(lambda addon: addon.search(term))
        if not isinstance(data, Mapping):
            raise CatalogError("invalid", "the add-on's search answer is not an object")
        return _reading(lambda: self._searched(space, data))

    def _searched(self, space: Space, data: Mapping[str, Any]) -> SearchResults:
        artists = _each(data.get("artists"), lambda item: read_artist(space, item))
        for artist in artists:
            self._named(space, artist.name, space.own(artist.ref.id[1:]))
        songs = _each(data.get("tracks"), lambda item: read_track(space, item))
        songs = tuple(self._with_album(song) for song in songs)
        self._keep(songs)
        albums = _each(data.get("albums"), lambda item: read_release(space, item))
        return SearchResults(artists, albums, songs)

    async def album(self, album_id: str) -> CatalogRelease:
        own = own_id(album_id[SERVICE_TAG:])
        if own is None:
            raise CatalogError("not_found", "not an album of the add-on")
        data, space = await self._ask(
            lambda addon: addon.album(own), service=album_id[:SERVICE_TAG]
        )
        release = _reading(lambda: read_album(space, own, data))
        self._keep(release.tracks)
        return release

    async def song(self, song_id: str) -> CatalogTrack:
        track = self._songs.get(song_id)
        if track is None:
            raise CatalogError(
                "not_found", "not in a recent answer of the add-on (it has no request for a song)"
            )
        self._songs.move_to_end(song_id)
        return track

    async def album_of(
        self, song_id: str, album: Callable[[str], Awaitable[CatalogRelease]] | None = None
    ) -> CatalogRef | None:
        """The album of a song whose answer named it by title only (asked when the song is
        added to the library): the album of that title that lists the song - searched for
        by title and artist, the song's artist's first, and opened to see (``CANDIDATES``
        at most), all within the catalog's timeout. ``album``: how albums are opened (the
        cache's, so the answer checked here is the one then written). None: not found."""
        track = self._songs.get(song_id)
        if track is None or track.album is not None or not track.album_title:
            return track.album if track is not None else None
        try:
            with anyio.fail_after(self.timeout):
                return await self._album_of(track, album or self.album)
        except TimeoutError:
            reason = "the song's album was not found in time"
            raise CatalogError("unavailable", reason) from None

    async def _album_of(
        self, track: CatalogTrack, album: Callable[[str], Awaitable[CatalogRelease]]
    ) -> CatalogRef | None:
        first = (credit_names(track.artist) or [""])[0]
        found = await self._found(f"{track.album_title} {first}".strip())
        wanted = fold(track.album_title)
        titled = [listed for listed in found.albums if fold(listed.title) == wanted]
        titled.sort(key=lambda listed: not same_artist(listed.artist, track.artist))
        for candidate in titled[:CANDIDATES]:
            try:
                release = await album(candidate.ref.id)
            except CatalogError as exc:
                if exc.kind != "not_found":
                    raise
                continue
            if any(listed.ref == track.ref for listed in release.tracks):
                self._keep(t for t in release.tracks if t.ref == track.ref)
                return release.ref
        return None

    async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
        return ()  # the protocol's ISRC lookup names no album: albums are matched by title

    async def _real(self, artist_id: str) -> tuple[str, str]:
        """The add-on's own ID of an artist item, and the tag of the service it is of: the
        ID the item carries, or - an artist by name - the one an answer named that name
        with, else the one a search for it finds (the credit's whole name, else its
        first)."""
        kind, rest = artist_id[:1], artist_id[1:]
        if kind == _ARTIST_ITEM and (own := own_id(rest[SERVICE_TAG:])) is not None:
            return own, rest[:SERVICE_TAG]
        value = own_id(rest) if kind == _ARTIST_NAME else None
        if value is None:
            raise CatalogError("not_found", "not an artist of the add-on")
        names = [value, *credit_names(value)[:1]]
        for name in names:
            if (known := self._artists.get(fold(name))) is not None:
                return known[1], known[0]  # (with its service: another one's is not found)
        found = await self._found(value)
        for name in names:
            for artist in found.artists:
                item = artist.ref.id[1:]
                own = own_id(item[SERVICE_TAG:])
                if own is not None and fold(artist.name) == fold(name):
                    return own, item[:SERVICE_TAG]
        raise CatalogError("not_found", "the add-on's search finds no artist of that name")

    async def _artist_page(self, own: str, service: str) -> _Page:
        data, space = await self._ask(lambda addon: addon.artist(own), service=service)
        return _reading(lambda: self._paged(space, own, data))

    def _paged(self, space: Space, own: str, data: Any) -> _Page:
        artist = read_artist(space, data, own=own)
        if artist is None:
            raise CatalogError("not_found", "the add-on has no such artist")
        self._named(space, artist.name, own)
        releases = _each(
            data.get("albums"),
            lambda item: read_release(space, item, artist=artist.name),
            MAX_ALBUMS,
        )
        tracks = _first(data, "topTracks", "tracks")
        top = _each(tracks, lambda item: read_track(space, item))
        top = tuple(self._with_album(song) for song in top)
        self._keep(top)
        return _Page(artist, releases, top)

    async def artist(self, artist_id: str) -> CatalogArtist:
        return (await self._page(*await self._real(artist_id))).artist

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        return (await self._page(*await self._real(artist_id))).releases

    async def top_songs(self, artist_id: str, limit: int = 10) -> tuple[CatalogTrack, ...]:
        return (await self._page(*await self._real(artist_id))).top[: max(0, limit)]

    async def artists_of(
        self, songs: tuple[str, ...], albums: tuple[str, ...]
    ) -> dict[str, tuple[CatalogRef, ...]]:
        return {}  # every item names its artists (by name)

    async def artwork(self, url: str) -> tuple[bytes, str]:
        address = unmarked(self.key, url)
        if address is None:
            raise CatalogError("invalid", "not an artwork address of this catalog")
        if self.registry is not None:
            await self.registry.enabled()  # (the add-ons' own addresses are known)
        at: list[pacing.AddonPace | None] = [None]  # the limit of the add-on the hop is at

        async def turn(hop: httpx.URL) -> None:
            at[0] = await self._own_pace(hop)

        try:
            with anyio.fail_after(self.timeout):
                # (Redirects are followed here, hop by hop: each may be an add-on's address.)
                async with pacing.get(
                    self.http, address, turn=turn, headers={"accept": "image/*"}
                ) as response:
                    return await self._image(response, at[0])
        except pacing.Blocked:
            raise CatalogError("rate_limited", pacing.BLOCKED) from None
        except TimeoutError:
            raise CatalogError("unavailable", "artwork: no answer in time") from None
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as exc:
            if denied(exc):
                raise CatalogError("invalid", NOT_PUBLIC) from None
            raise CatalogError("unavailable", f"artwork: {type(exc).__name__}") from None

    async def _own_pace(self, url: httpx.URL) -> pacing.AddonPace | None:
        """A turn at an add-on's request limit for a cover request that goes to an add-on's
        own address (covers on image hosts take none): as background work, after the views
        - and only for an address covers are fetched from at all (a public one: an add-on
        on this machine or network spends nothing on covers that are never fetched)."""
        if self.registry is None:
            return None
        pace = self.registry.paces.own(str(url))
        if pace is None:
            return None
        try:
            found = await self.resolver(url.host, url.port or (80 if url.scheme == "http" else 443))
        except OSError:
            found = []
        if not any(allowed(ip, Reach.PUBLIC) for ip in found):
            raise CatalogError("invalid", NOT_PUBLIC)
        with pacing.urgent(pacing.current() or pacing.Urgency(pacing.WARM)):
            await pace.request()
        return pace

    @staticmethod
    async def _image(response: httpx.Response, pace: pacing.AddonPace | None) -> tuple[bytes, str]:
        status = response.status_code
        if status in (404, 410):
            raise CatalogError("not_found", f"artwork: HTTP {status}")
        if status == 429:
            if pace is not None:  # an add-on itself said so: it is left alone
                pace.limited(pacing.retry_after(response.headers.get("retry-after")))
            raise CatalogError("rate_limited", "artwork: HTTP 429")
        if status != 200:
            raise CatalogError("unavailable", f"artwork: HTTP {status}")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_ARTWORK_BYTES:
                raise CatalogError("invalid", "artwork: answer too large")
        kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        return bytes(body), kind or "application/octet-stream"


# --- the kind's declaration -------------------------------------------------------------------


class Settings(BaseModel):
    """The kind's own setting in ``[catalog]``."""

    # The name of the add-on to take the catalog from, as the Add-ons page (or
    # ``[[addons]]``) has it.
    addon: str = Field(default="", max_length=200)


def problem(settings: Any) -> Problem | None:
    if not str(getattr(settings, "addon", "") or "").strip():
        return Problem(
            "addon",
            "Enter the name of the add-on to take the catalog from.",
            "is needed: the name of the add-on to take the catalog from.",
        )
    return None


def build(settings: Any, context: Context) -> AddonCatalog:
    if problem(settings) is not None:
        raise ValueError(
            'the catalog kind "addon" needs [catalog] addon: the name of the add-on to'
            " take the catalog from"
        )
    return AddonCatalog(
        str(settings.addon),
        context.addons,
        context.http(),
        timeout=context.timeout_seconds,
        ttl=float(getattr(settings, "cache_seconds", 3600.0)),
        resolver=context.resolver,
    )


adapter = Adapter(
    label=LABEL,
    build=build,
    settings=Settings,
    words={
        "addon": Words(
            "Add-on",
            "The name of the add-on to take the catalog from, as the Add-ons page has it."
            " It needs a catalog of its own: a search, albums and artists (not every"
            " add-on has one). Its requests go to that add-on like its audio requests: with"
            " its settings, and at its request limit. Another name is another catalog.",
            max_length=200,
            choices="sources",
        ),
    },
    problem=problem,
)
