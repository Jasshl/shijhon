"""Songs and artists listened together, as ListenBrainz knows them from its users' listening
sessions: the second source of similarity next to Last.fm's (``similar.py``), for radio from
catalog items, discovery mixes' candidates and the agents' ``find_listened_together``.

On unless ``[listenbrainz] enabled`` is off, for everyone, without an account. A request
names an artist and a song, or the MusicBrainz IDs ListenBrainz gave for them, and nothing
else: no account, no listening history. The answers are names; what they are in the library
or the catalog is found elsewhere.

- Names to a recording. Only a song's canonical MusicBrainz recording has similar
  recordings, so a song is tried in up to three steps, each one request, until a recording
  with similar recordings is found: ListenBrainz Labs' artist credit and recording lookup of
  the exact names (punctuation, accents and capitals aside; several names in one request);
  with a token (``[listenbrainz] token``, an administrator's, for everyone) ListenBrainz's
  own metadata lookup, which matches names more loosely; and Labs' recording search for the
  song's lead artist and title, of whose results only one credited to that lead artist with
  that title (both folded, the title without qualifiers such as "(Radio Edit)") and not
  tried yet is taken - the one whose credit names most of the song's featured artists. The
  exact lookup often finds a compilation's or a single's copy of a song with a featured
  credit, which has no similar recordings: the search finds the original.
- A recording to similar recordings, and an artist to similar artists: Labs' session-based
  lists, the most similar first. An artist's MusicBrainz ID is told by the lookup of a few
  of its songs.

Labs is experimental, so every answer is optional and nothing waits for it long. ListenBrainz
asks an application for at most one request a second: requests are paced so, for everything
together, and each one is bounded by ``timeout_seconds`` with its wait for a turn. A request
that cannot get its turn in time is given up without blaming ListenBrainz; one that gets its
turn is sent only if ListenBrainz is still on and not resting. A failure, an unreadable
answer or a rate limit rests ListenBrainz - a rate limit for as long as it says, within
bounds - and while it rests nothing is asked: callers get :class:`ListeningUnavailable` at
once and go on with Last.fm's answer, or none. Answers, also "nothing known", are kept in
memory for a day (similarity changes slowly); failures are not, and neither is what was
looked up while the token was refused. A changed token forgets what depended on it. The
token is sent only to api.listenbrainz.org and is never logged; logs never carry a name
either.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import anyio
import httpx
from aiolimiter import AsyncLimiter
from async_lru import alru_cache

from shijhon.config import ListenBrainzSettings
from shijhon.delivery.netpolicy import USER_AGENT
from shijhon.matching.normalize import core_title, featured_names, fold
from shijhon.playlists.model import is_text

log = logging.getLogger(__name__)

API = "https://api.listenbrainz.org"
LABS = "https://labs.api.listenbrainz.org"
# Labs' algorithms (the names it accepts): sessions over about twenty years of listens.
SONGS_ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_15_limit_50_skip_30"
ARTISTS_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
)
BODY_LIMIT = 2_000_000
CACHE_SECONDS = 24 * 3600.0
CACHE_SIZE = 4096
REST_SECONDS = 60.0  # nothing is asked for this long after a failure
LONGEST_REST = 600.0  # ... nor longer than this after a rate limit, whatever it says
LOOKUP_ROWS = 50  # names in one Labs lookup at most
SEARCH_ROWS = 10  # results of a recording search that are looked at
NAME_LENGTH = 300
TITLES = 5  # an artist's song titles looked up to tell its MusicBrainz ID
MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Words of a title that mark another version of a song, and of a credit that mark another
# artist's rendition of it: a search result with one the song's own names lack is not it.
VERSION_WORDS = re.compile(
    r"\b(live|karaoke|instrumental|covers?|tribute|remix(?:es|ed)?|rmx|acoustic|demo|"
    r"unplugged|a ?cappella|acapella|orchestral|symphonic|rehearsal|reprise|lullaby|"
    r"8[- ]bit|re-?recorded|sped up|slowed)\b",
    re.IGNORECASE,
)
RENDITION_WORDS = re.compile(
    r"\b(karaoke|tribute|covers?|made famous|in the style of|originally performed)\b",
    re.IGNORECASE,
)

type Names = tuple[tuple[str, str], ...]  # (artist, title) of a song, or of its variants
type Similar = tuple[tuple[str, str, float], ...]  # (artist, title, score: the first's 1.0)


class ListeningUnavailable(Exception):
    """ListenBrainz is off, resting, busy or did not answer usably: nothing is known now."""


class _Refused(Exception):
    """The token was refused (the metadata lookup only)."""


@dataclass(frozen=True)
class Recording:
    mbid: str
    artists: tuple[str, ...]  # its artists' MusicBrainz IDs
    credit: str  # its artist credit, as MusicBrainz writes it


class _Kept[K, V]:
    """Answers kept for a day, the newest ``CACHE_SIZE``."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.items: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> tuple[V] | None:
        """(The answer,), or None when none is kept."""
        found = self.items.get(key)
        if found is None or self.clock() - found[0] > CACHE_SECONDS:
            self.items.pop(key, None)
            return None
        self.items.move_to_end(key)
        return (found[1],)

    def put(self, key: K, value: V) -> None:
        self.items[key] = (self.clock(), value)
        self.items.move_to_end(key)
        while len(self.items) > CACHE_SIZE:
            self.items.popitem(last=False)

    def clear(self) -> None:
        self.items.clear()


def clean(value: Any) -> str:
    """A name fit to send or show, or "" (controls and line breaks never are)."""
    name = " ".join(str(value or "").split())[:NAME_LENGTH]
    return name if is_text(name) else ""


def lead(credit: str) -> str:
    """A credit's lead artist: without the artists it features."""
    return (featured_names(credit) or [credit])[0].strip()


def _marks(pattern: re.Pattern[str], text: str) -> set[str]:
    return {" ".join(m.lower().replace("-", " ").split()) for m in pattern.findall(text)}


def same_version(wanted_credit: str, wanted_title: str, credit: str, title: str) -> bool:
    """Whether a search result is the song itself as far as its names tell: no version
    word in its title (live, karaoke, remix...) and no rendition word in its credit
    (karaoke, tribute, covers...) that the song's own names lack."""
    return _marks(VERSION_WORDS, title) <= _marks(VERSION_WORDS, wanted_title) and _marks(
        RENDITION_WORDS, credit
    ) <= _marks(RENDITION_WORDS, wanted_credit)


def _mbid(value: Any) -> str:
    return value if isinstance(value, str) and MBID.fullmatch(value) else ""


def _recording(row: Any) -> Recording | None:
    if not isinstance(row, dict) or not (mbid := _mbid(row.get("recording_mbid"))):
        return None
    listed = row.get("artist_mbids")
    artists = tuple(a for a in listed if _mbid(a)) if isinstance(listed, list) else ()
    return Recording(mbid, artists, clean(row.get("artist_credit_name")))


def _score(row: dict[str, Any]) -> float:
    try:
        score = float(row.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0
    return score if score > 0 else 0.0


def _song_key(artist: str, title: str) -> tuple[str, str]:
    return fold(artist), fold(title)


class ListenBrainz:
    # The HTTP transport; None: the network. (The tests set a stand-in.)
    transport: ClassVar[httpx.AsyncBaseTransport | None] = None

    def __init__(
        self,
        settings: ListenBrainzSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings  # read at every request: the dashboard changes it at once
        self.clock = clock
        self.http = httpx.AsyncClient(
            transport=transport or self.transport,
            trust_env=False,
            timeout=30.0,  # (each request is bounded by timeout_seconds as a whole)
            headers={"user-agent": USER_AGENT},
        )
        self.pace = AsyncLimiter(1, 1)  # one request a second, as ListenBrainz asks
        self.resting_until = 0.0
        self.requests = 0  # observable in tests
        self._refused = ""  # the fingerprint of the token last refused (said once)
        self._token = self._fingerprint()  # ... of the token the answers below were found with
        cached = alru_cache(maxsize=CACHE_SIZE, ttl=CACHE_SECONDS)
        self._exact: _Kept[tuple[str, str], Recording | None] = _Kept(clock)
        self._metadata = cached(self._ask_metadata)  # (keyed by the token's fingerprint)
        self._search = cached(self._ask_search)
        self._similar_songs = cached(self._ask_similar_songs)
        self._artist_ids = cached(self._ask_artist_id)
        self._similar_artists = cached(self._ask_similar_artists)
        self._songs: _Kept[Names, Similar] = _Kept(clock)  # a song's answer, by its names
        self._artists: _Kept[str, tuple[str, ...]] = _Kept(clock)  # an artist's, by name

    @property
    def on(self) -> bool:
        return self.settings.enabled

    @property
    def available(self) -> bool:
        """On and not resting: a request now would be sent."""
        return self.on and self.clock() >= self.resting_until

    async def aclose(self) -> None:
        await self.http.aclose()

    # --- what callers ask -------------------------------------------------------------------

    def known_songs(self, names: Sequence[tuple[str, str]]) -> Similar | None:
        """The answer of ``similar_songs`` for these names when one is kept: nothing asked."""
        self._token_check()
        found = self._songs.get(tuple(names))
        return found[0] if found is not None else None

    def known_artists(self, name: str) -> tuple[str, ...] | None:
        """The answer of ``similar_artists`` for this name when one is kept."""
        found = self._artists.get(fold(name))
        return found[0] if found is not None else None

    async def similar_songs(self, names: Sequence[tuple[str, str]], limit: int) -> Similar:
        """The songs most often listened together with this one, the most similar first;
        none for a song ListenBrainz does not know. ``names``: the song's (artist, title)
        as the catalog writes it, then variants of it."""
        return (await self.similar_songs_kept(names, limit))[0]

    async def similar_songs_kept(
        self, names: Sequence[tuple[str, str]], limit: int
    ) -> tuple[Similar, bool]:
        """(``similar_songs``, whether the answer may be kept): not when it was found while
        the token was refused or changed - a valid token may find more."""
        self._token_check()
        wanted = tuple(names)[:LOOKUP_ROWS]
        if (kept := self._songs.get(wanted)) is not None:
            return kept[0][:limit], True
        token = self._token
        found, keep = await self._songs_of(wanted)
        keep = keep and token == self._token
        if keep:
            self._songs.put(wanted, found)
        return found[:limit], keep

    async def similar_to_many(
        self, songs: Sequence[tuple[str, str]], found: MutableMapping[int, Similar]
    ) -> None:
        """The similar songs of each of several songs, by their position, into ``found``
        as they come (what came stays when the caller stops waiting). Their exact lookups
        go in one request. Songs ListenBrainz does not know have none; a failure ends the
        rest."""
        self._token_check()
        for start in range(0, len(songs), LOOKUP_ROWS):
            await self._lookup(tuple(songs[start : start + LOOKUP_ROWS]))
        for at, song in enumerate(songs):
            if similar := await self.similar_songs([song], LOOKUP_ROWS * 2):
                found[at] = similar

    async def similar_artists(self, name: str, titles: Sequence[str], limit: int) -> list[str]:
        """The names of the artists most often listened together with this one, the most
        similar first; none for an artist ListenBrainz does not know. ``titles``: some of
        its songs, which tell who it is."""
        if (kept := self._artists.get(fold(name))) is not None:
            return list(kept[0])[:limit]
        wanted = tuple(dict.fromkeys(t for t in (clean(t) for t in titles) if t))[:TITLES]
        if not clean(name) or not wanted:
            return []
        artist = await self._artist_ids(clean(name), wanted)
        found = await self._similar_artists(artist) if artist else ()
        self._artists.put(fold(name), found)
        return list(found)[:limit]

    # --- a song's recording -----------------------------------------------------------------

    async def _songs_of(self, names: Names) -> tuple[Similar, bool]:
        """(The similar songs of the first recording of the song that has some, whether
        the answer may be kept): the exact lookup, the loose one (with a token), the
        search - each step only when the ones before found no recording with any."""
        tried: set[str] = set()
        keep = True
        for step in ("exact", "loose", "search"):
            try:
                if step == "exact":
                    found = await self._first_exact(names)
                elif step == "loose":
                    found = await self._metadata_lookup(names)
                else:
                    found = await self._searched(names, tried)
            except _Refused:
                keep = False  # (found again with a valid token)
                continue
            if found is None or found.mbid in tried:
                continue
            tried.add(found.mbid)
            if similar := await self._similar_songs(found.mbid):
                return similar, keep
        return (), keep

    async def _first_exact(self, names: Names) -> Recording | None:
        found = await self._lookup(names)
        return next((r for r in found if r is not None), None)

    async def _lookup(self, names: Names) -> list[Recording | None]:
        """Labs' artist credit and recording lookup of each (artist, title); the names not
        kept from before in one request."""
        rows = [(at, clean(a), clean(t)) for at, (a, t) in enumerate(names)]
        found: list[Recording | None] = [None] * len(names)
        asked: list[tuple[int, str, str]] = []
        for at, artist, title in rows:
            if not artist or not title:
                continue
            if (kept := self._exact.get(_song_key(artist, title))) is not None:
                found[at] = kept[0]
            elif (artist, title) not in {(a, t) for _, a, t in asked}:
                asked.append((at, artist, title))
        if not asked:
            return found
        data = await self._request(
            "POST",
            f"{LABS}/acr-lookup/json",
            body=[{"artist_credit_name": a, "recording_name": t} for _, a, t in asked],
        )
        if not isinstance(data, list):
            raise self._failed("an unreadable answer")
        answered: dict[int, Recording | None] = {}
        for row in data:
            index = row.get("index") if isinstance(row, dict) else None
            if isinstance(index, int) and 0 <= index < len(asked):
                answered[index] = _recording(row)
        for index, (_, artist, title) in enumerate(asked):
            self._exact.put(_song_key(artist, title), answered.get(index))
        for at, artist, title in rows:
            if found[at] is None and artist and title:
                kept = self._exact.get(_song_key(artist, title))
                found[at] = kept[0] if kept is not None else None
        return found

    async def _metadata_lookup(self, names: Names) -> Recording | None:
        """ListenBrainz's metadata lookup of the song as the catalog names it (it needs the
        token); None without one. :class:`_Refused` when the token is refused."""
        if not self.settings.api_token() or not names:
            return None
        if self._fingerprint() == self._refused:
            raise _Refused  # (not asked again with it; nothing found is kept)
        artist, title = clean(names[0][0]), clean(names[0][1])
        if not artist or not title:
            return None
        return await self._metadata(self._fingerprint(), artist, title)

    async def _ask_metadata(self, fingerprint: str, artist: str, title: str) -> Recording | None:
        """(Kept by the token's fingerprint with the names: a refusal raises, so it is not
        kept, and another token asks again.)"""
        try:
            data = await self._request(
                "GET",
                f"{API}/1/metadata/lookup/",
                params={"artist_name": artist, "recording_name": title},
                token=self.settings.api_token(),
            )
        except _Refused:
            if self._refused != fingerprint:
                self._refused = fingerprint
                log.warning(
                    "listenbrainz: the token was refused; enter a valid one on the"
                    " ListenBrainz page (songs are looked up without it meanwhile)"
                )
            raise
        if not isinstance(data, dict):
            raise self._failed("an unreadable answer")
        return _recording(data)

    async def _searched(self, names: Names, tried: set[str]) -> Recording | None:
        """Labs' recording search for the song's lead artist and title: of its results
        credited to that lead artist with that title and not tried yet, the one whose credit
        names most of the song's featured artists, the first among equals. A result marked as
        another version (live, karaoke, a remix...) or another artist's rendition (karaoke,
        tribute, covers...) that the song's own names do not mark is never taken."""
        if not names:
            return None
        credit, full = clean(names[0][0]), clean(names[0][1])
        artist, title = lead(credit), core_title(full) or ""
        if not fold(artist) or not fold(title):
            return None
        featured = [fold(name) for name in featured_names(credit)[1:] if fold(name)]
        best: tuple[int, Recording] | None = None
        for row in await self._search(f"{artist} {title}"):
            mbid = _mbid(row.get("recording_mbid"))
            found, named_title = (
                clean(row.get("artist_credit_name")),
                clean(row.get("recording_name")),
            )
            if (
                mbid
                and mbid not in tried
                and fold(lead(found)) == fold(artist)
                and fold(core_title(named_title)) == fold(title)
                and same_version(credit, full, found, named_title)
            ):
                named = sum(1 for name in featured if name in fold(found))
                if best is None or named > best[0]:
                    best = (named, Recording(mbid, (), found))
        return best[1] if best is not None else None

    async def _ask_search(self, query: str) -> tuple[dict[str, Any], ...]:
        data = await self._request("GET", f"{LABS}/recording-search/json", params={"query": query})
        if not isinstance(data, list):
            raise self._failed("an unreadable answer")
        return tuple(row for row in data[:SEARCH_ROWS] if isinstance(row, dict))

    # --- similar recordings and artists -----------------------------------------------------

    async def _ask_similar_songs(self, mbid: str) -> Similar:
        data = await self._request(
            "GET",
            f"{LABS}/similar-recordings/json",
            params={"recording_mbids": mbid, "algorithm": SONGS_ALGORITHM},
        )
        if not isinstance(data, list):
            raise self._failed("an unreadable answer")
        rows = [r for r in data if isinstance(r, dict) and r.get("recording_mbid") != mbid]
        top = max((_score(r) for r in rows), default=0.0) or 1.0
        found: dict[tuple[str, str], float] = {}
        for row in sorted(rows, key=lambda r: -_score(r)):
            pair = (clean(row.get("artist_credit_name")), clean(row.get("recording_name")))
            if all(pair):
                found.setdefault(pair, round(_score(row) / top, 4))
        return tuple((artist, title, score) for (artist, title), score in found.items())

    async def _ask_artist_id(self, name: str, titles: tuple[str, ...]) -> str | None:
        """The MusicBrainz ID of the one artist the lookup of these songs credits under
        this name, most often."""
        votes = Counter(
            found.artists[0]
            for found in await self._lookup(tuple((name, title) for title in titles))
            if found is not None and len(found.artists) == 1 and fold(found.credit) == fold(name)
        )
        return votes.most_common(1)[0][0] if votes else None

    async def _ask_similar_artists(self, mbid: str) -> tuple[str, ...]:
        data = await self._request(
            "GET",
            f"{LABS}/similar-artists/json",
            params={"artist_mbids": mbid, "algorithm": ARTISTS_ALGORITHM},
        )
        if not isinstance(data, list):
            raise self._failed("an unreadable answer")
        rows = [r for r in data if isinstance(r, dict) and r.get("artist_mbid") != mbid]
        found: dict[str, str] = {}
        for row in sorted(rows, key=lambda r: -_score(r)):
            if name := clean(row.get("name")):
                found.setdefault(fold(name), name)
        return tuple(found.values())

    # --- the token --------------------------------------------------------------------------

    def token_fingerprint(self) -> str:
        """Tells tokens apart ("" without one) without being the token: what was saved
        with one token is not used with another."""
        return self._fingerprint()

    def _fingerprint(self) -> str:
        """Of the token in use ("" without one): never the token itself."""
        token = self.settings.api_token()
        return hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""

    def _token_check(self) -> None:
        """A changed token: what was found with the old one (or without one) is forgotten."""
        fingerprint = self._fingerprint()
        if fingerprint != self._token:
            self._token = fingerprint
            self._metadata.cache_clear()
            self._songs.clear()

    # --- the requests -----------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
        token: str = "",
    ) -> Any:
        if not self.available:
            raise ListeningUnavailable
        deadline = anyio.current_time() + self.settings.timeout_seconds
        try:
            with anyio.fail_after(self.settings.timeout_seconds):
                await self.pace.acquire()
        except TimeoutError:  # busy with others' requests: no failure of ListenBrainz
            raise ListeningUnavailable from None
        # Turned off, or resting after another request's failure, while this one waited.
        if not self.available:
            raise ListeningUnavailable
        self.requests += 1
        headers = {"authorization": f"Token {token}"} if token else {}
        try:
            with anyio.fail_after(max(0.0, deadline - anyio.current_time())):
                async with self.http.stream(
                    method, url, params=params, json=body, headers=headers
                ) as response:
                    self._limits(response)
                    if token and response.status_code in (401, 403):
                        raise _Refused
                    if response.status_code != 200:
                        raise self._failed(f"HTTP {response.status_code}")
                    content = bytearray()
                    async for part in response.aiter_bytes():
                        content.extend(part)
                        if len(content) > BODY_LIMIT:
                            raise self._failed("an answer too large")
            return json.loads(content)
        except (_Refused, ListeningUnavailable):
            raise
        except TimeoutError:
            raise self._failed("no answer in time") from None
        except httpx.HTTPError as exc:
            raise self._failed(type(exc).__name__) from None
        except ValueError:
            raise self._failed("an unreadable answer") from None

    def _limits(self, response: httpx.Response) -> None:
        """A rate limit rests ListenBrainz for as long as it says (``X-RateLimit-Reset-In``),
        also when this answer was the last one allowed."""
        limited = response.status_code == 429
        if not limited and response.headers.get("x-ratelimit-remaining", "").strip() != "0":
            return
        try:
            seconds = float(response.headers.get("x-ratelimit-reset-in", REST_SECONDS))
        except ValueError:
            seconds = REST_SECONDS
        seconds = min(LONGEST_REST, max(1.0, seconds)) if seconds == seconds else REST_SECONDS
        self._rest(seconds, "a rate limit")
        if limited:
            raise ListeningUnavailable

    def _failed(self, why: str) -> ListeningUnavailable:
        self._rest(REST_SECONDS, why)
        return ListeningUnavailable()

    def _rest(self, seconds: float, why: str) -> None:
        until = self.clock() + seconds
        if until > self.resting_until:
            if self.clock() >= self.resting_until:  # (said when a rest begins)
                log.info("listenbrainz: %s; not asked for %gs", why, seconds)
            self.resting_until = until
