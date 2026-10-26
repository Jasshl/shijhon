"""Hints about songs for described playlists: year and genres as the library or the catalog
give them, community tags from Last.fm, the sound analyzer's facts and where a song comes
from. Every source is optional and bounded in requests and time; a missing, failing or
slow source means fewer hints, never a failed playlist."""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal

import anyio
import httpx
from aiolimiter import AsyncLimiter

from shijhon.matching.normalize import fold, recording_key
from shijhon.playlists.history import ENDPOINT, History, items
from shijhon.playlists.model import Connection, PlaylistError, Track, is_text
from shijhon.playlists.sound import FEATURES, SongSound
from shijhon.store import Store
from shijhon.views.ids import CatalogId

if TYPE_CHECKING:
    from shijhon.playlists.service import Playlists

log = logging.getLogger(__name__)

TAG_TTL = 30 * 86400
TAG_SECONDS = 5  # one Last.fm request
TAG_BUDGET = 15  # one batch of lookups, including the pace between requests
TAG_REQUESTS = 120  # Last.fm requests in one generation; cached tags cost none
TAGS_KEPT = 5
CONCURRENCY = 4
SOUND_SECONDS = 15  # one call to the sound analyzer, or one batch of its songs' reads
# Personal labels rather than descriptions of the music.
JUNK_TAGS = frozenset(
    {"seen live", "favorite", "favorites", "favourite", "favourites", "albums i own"}
)

Source = Literal["own", "added", "catalog"]
Status = Literal["used", "off", "unavailable"]


class TagsUnavailable(Exception):
    """Last.fm did not answer usably: no tags now, and none cached."""


def tag_key(artist: str, title: str = "") -> tuple[str, str]:
    """The cache key: folded artist and recording title; an empty title is the artist."""
    return fold(artist), recording_key(title) if title else ""


def top_tags(value: Any, artist: str) -> list[str]:
    """Last.fm lists tags by weight (0-100); keep a few that describe the music."""
    kept: list[str] = []
    for tag in items(value):
        name = tag.get("name")
        try:
            count = int(tag.get("count", 0))
        except (TypeError, ValueError):
            continue
        if not isinstance(name, str) or count < 10:
            continue
        name = " ".join(name.split()).casefold()
        if (
            not name
            or len(name) > 40
            or not is_text(name)
            or name in JUNK_TAGS
            or fold(name) == fold(artist)
            or name in kept
        ):
            continue
        kept.append(name)
        if len(kept) == TAGS_KEPT:
            break
    return kept


class Tags:
    """Community tags from Last.fm with the owner's key, cached for every account."""

    def __init__(self, store: Store, history: History) -> None:
        # Last.fm asks for at most five requests a second; history imports pace apart.
        self.store, self.history = store, history
        self.pace = AsyncLimiter(CONCURRENCY, 1)

    async def cached(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], list[str]]:
        wanted = set(keys)
        artists = sorted({artist for artist, _ in wanted})
        found: dict[tuple[str, str], list[str]] = {}
        for start in range(0, len(artists), 500):
            chunk = artists[start : start + 500]
            rows = await self.store.fetchall(
                "SELECT artist,title,tags FROM music_tags WHERE fetched>=? AND artist IN"  # noqa: S608
                f" ({','.join('?' for _ in chunk)})",
                [time.time() - TAG_TTL, *chunk],
            )
            for row in rows:
                key = (row["artist"], row["title"])
                if key in wanted:
                    try:
                        tags = json.loads(row["tags"])
                    except ValueError:
                        continue
                    if isinstance(tags, list):
                        found[key] = [t for t in tags if isinstance(t, str)][:TAGS_KEPT]
        return found

    async def save(self, found: dict[tuple[str, str], list[str]]) -> None:
        if not found:
            return
        now = time.time()
        async with self.store.transaction() as db:
            await db.executemany(
                "INSERT OR REPLACE INTO music_tags(artist,title,tags,fetched) VALUES (?,?,?,?)",
                [(artist, title, json.dumps(tags), now) for (artist, title), tags in found.items()],
            )

    async def request(self, connection: Connection, method: str, **params: str) -> list[str]:
        """The song's or artist's top tags; none when Last.fm does not know it."""
        async with self.pace:
            try:
                with anyio.fail_after(TAG_SECONDS):
                    async with self.history.http.stream(
                        "GET",
                        ENDPOINT,
                        params={
                            "api_key": connection.history_key.get_secret_value(),
                            "method": method,
                            "format": "json",
                            "autocorrect": "1",
                            **params,
                        },
                    ) as response:
                        body = bytearray()
                        async for part in response.aiter_bytes():
                            body.extend(part)
                            if len(body) > 500_000:
                                raise ValueError
                data = json.loads(body)
            except (httpx.HTTPError, ValueError, TimeoutError):
                raise TagsUnavailable from None
        if not isinstance(data, dict):
            raise TagsUnavailable
        if data.get("error") == 6:  # Last.fm's "not found"
            return []
        if data.get("error") or response.status_code != 200:
            raise TagsUnavailable
        tags = data.get("toptags")
        return top_tags(tags.get("tag"), params["artist"]) if isinstance(tags, dict) else []


def sound_facts(sound: SongSound) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if sound.tempo:
        data["tempo"] = round(sound.tempo)
    if sound.key:
        data["key"] = sound.key[:20]
    if sound.energy is not None:
        data["energy"] = sound.energy
    features = {k: v for k, v in sound.features.items() if k in FEATURES}
    if features:
        data["features"] = features
    labels = [label[:40] for label in sound.labels if label][:5]
    if labels:
        data["labels"] = labels
    return data


class Facts:
    """The hints of one generation, gathered once per song."""

    def __init__(self, service: Playlists, connection: Connection) -> None:
        self.service, self.connection = service, connection
        self.tags: dict[tuple[str, str], list[str]] = {}
        self.song_tags: dict[str, list[str]] = {}
        self.sound: dict[str, SongSound] = {}
        self.sources: dict[str, Source] = {}
        # Library songs the sound analyzer suggested, by how.
        self.suggested: dict[str, str] = {}
        self.requests = TAG_REQUESTS
        self.tag_status: Status = "used" if connection.history_key.get_secret_value() else "off"
        # Which kinds of sound call failed in this generation: "facts" reads Shijhon's
        # own synced copy, "live" asks the analyzer itself; one failing leaves the other.
        self.sound_failed: set[str] = set()

    @property
    def sound_status(self) -> Status:
        if self.service.sound is None:
            return "off"
        return "unavailable" if self.sound_failed else "used"

    async def describe(self, tracks: Sequence[Track]) -> None:
        new = list({t.id: t for t in tracks if t.id not in self.sources}.values())
        if not new:
            return
        await self.label(new)
        async with anyio.create_task_group() as group:
            group.start_soon(self.lookup_tags, new)
            group.start_soon(self.lookup_sound, new)

    async def label(self, tracks: list[Track]) -> None:
        """Shijhon's own records tell what it added: a placeholder, delivered or not."""
        native = [t.id for t in tracks if CatalogId.parse(t.id) is None]
        added: set[str] = set()
        for start in range(0, len(native), 500):
            chunk = native[start : start + 500]
            rows = await self.service.repo.store.fetchall(
                "SELECT song_id FROM placeholders WHERE song_id IN"  # noqa: S608
                f" ({','.join('?' for _ in chunk)})",
                chunk,
            )
            added.update(row["song_id"] for row in rows)
        for track in tracks:
            self.sources[track.id] = (
                "catalog"
                if CatalogId.parse(track.id) is not None
                else "added"
                if track.id in added
                else "own"
            )

    async def lookup_sound(self, tracks: list[Track]) -> None:
        sound = self.service.sound
        ids = [t.id for t in tracks if CatalogId.parse(t.id) is None]
        if sound is None or not ids:
            return
        found = await self.bounded_sound(lambda: sound.facts(ids), kind="facts")
        if isinstance(found, dict):
            self.sound.update(
                (k, v) for k, v in found.items() if k in ids and isinstance(v, SongSound)
            )

    async def bounded_sound(self, call: Callable[[], Awaitable[Any]], *, kind: str = "live") -> Any:
        """A sound answer, or None when it is unavailable, slow or broken. After a failure
        of its ``kind`` ("facts" or "live"), that kind is not asked again this generation."""
        if kind in self.sound_failed:
            return None
        try:
            with anyio.fail_after(SOUND_SECONDS):
                return await call()
        except Exception as exc:
            # SoundUnavailable, a timeout, or an unexpected answer: curation goes on.
            log.warning("playlist sound %s unavailable: %s", kind, type(exc).__name__)
            self.sound_failed.add(kind)
            return None

    async def lookup_tags(
        self, tracks: Sequence[Track], *, cached_only: bool = False, budget: float | None = None
    ) -> None:
        """Tags for the songs, cached ones first. ``cached_only`` asks Last.fm for nothing;
        ``budget`` is the most seconds the requests may take together."""
        if self.tag_status != "used":
            return
        budget = TAG_BUDGET if budget is None else budget
        songs = {t.id: (tag_key(t.artist, t.title), tag_key(t.artist)) for t in tracks}
        wanted = {k for pair in songs.values() for k in pair} - self.tags.keys()
        self.tags.update(await self.service.tags.cached(wanted))
        fetched: dict[tuple[str, str], list[str]] = {}

        async def fetch(key: tuple[str, str], method: str, params: dict[str, str]) -> None:
            if self.requests <= 0 or self.tag_status != "used":
                return
            self.requests -= 1
            try:
                fetched[key] = await self.service.tags.request(self.connection, method, **params)
            except TagsUnavailable:
                self.tag_status = "unavailable"

        async def batch(work: dict[tuple[str, str], tuple[str, dict[str, str]]]) -> None:
            limiter = anyio.CapacityLimiter(CONCURRENCY)

            async def one(key: tuple[str, str], method: str, params: dict[str, str]) -> None:
                async with limiter:
                    await fetch(key, method, params)

            async with anyio.create_task_group() as group:
                for key, (method, params) in work.items():
                    group.start_soon(one, key, method, params)

        with anyio.move_on_after(budget) as scope:
            if not cached_only:
                first: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}
                for t in tracks:
                    song = songs[t.id][0]
                    if song not in self.tags:
                        first.setdefault(
                            song, ("track.getTopTags", {"artist": t.artist, "track": t.title})
                        )
                await batch(first)
                self.tags.update(fetched)
                # An artist's tags stand in for a song Last.fm knows no tags for.
                second: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}
                for t in tracks:
                    song, artist = songs[t.id]
                    if song in self.tags and not self.tags[song] and artist not in self.tags:
                        second.setdefault(artist, ("artist.getTopTags", {"artist": t.artist}))
                await batch(second)
        if scope.cancelled_caught:
            # The pace keeps a long batch from finishing in time: the songs left have no
            # tags this time. A request that timed out has marked Last.fm unavailable.
            log.info("playlist tags: lookups stopped at the time budget")
        self.tags.update(fetched)
        await self.service.tags.save(fetched)
        for ident, (song, artist) in songs.items():
            tags = self.tags.get(song) or self.tags.get(artist)
            if tags:
                self.song_tags[ident] = tags

    def shown(self, track: Track) -> dict[str, Any]:
        """The compact hints for one song, empty ones left out."""
        data: dict[str, Any] = {}
        if track.year:
            data["year"] = track.year
        if track.genres:
            data["genres"] = track.genres
        if tags := self.song_tags.get(track.id):
            data["tags"] = tags
        if (sound := self.sound.get(track.id)) and (compact := sound_facts(sound)):
            data["sound"] = compact
        data["source"] = self.sources.get(track.id) or (
            "catalog" if CatalogId.parse(track.id) else "own"
        )
        if label := self.suggested.get(track.id):
            data["suggested_by"] = label
        return data

    def counts(self, tracks: Sequence[Track]) -> dict[str, Any]:
        return {
            "songs": len(tracks),
            "with_sound": sum(t.id in self.sound for t in tracks),
            "with_tags": sum(t.id in self.song_tags for t in tracks),
            "sound": self.sound_status,
            "tags": self.tag_status,
        }

    def profile(self, tracks: Sequence[Track]) -> dict[str, Any]:
        """What the reference songs have in common, from the facts that exist."""
        sounds = [self.sound[t.id] for t in tracks if t.id in self.sound]
        result: dict[str, Any] = {"songs": len(tracks), "with_sound": len(sounds)}
        if energies := [s.energy for s in sounds if s.energy is not None]:
            result["energy"] = round(statistics.median(energies))
        if tempos := [s.tempo for s in sounds if s.tempo]:
            result["tempo"] = round(statistics.median(tempos))
        features = {
            name: round(statistics.median(values))
            for name in FEATURES
            if (values := [s.features[name] for s in sounds if name in s.features])
        }
        if features:
            result["features"] = features
        tags = Counter(tag for t in tracks for tag in self.song_tags.get(t.id, []))
        if tags:
            result["tags"] = [tag for tag, _ in tags.most_common(8)]
        genres = Counter(genre.casefold() for t in tracks for genre in t.genres)
        if genres:
            result["genres"] = [genre for genre, _ in genres.most_common(5)]
        if years := [t.year for t in tracks if t.year]:
            result["years"] = [min(years), max(years)]
        return result


async def library_songs(
    service: Playlists, connection: Connection, ids: Sequence[str]
) -> list[Track]:
    """The sound analyzer's library songs as the caller may read them in this library;
    unreadable ones are left out. Bounded in time and concurrency, order kept."""
    found: dict[str, Track] = {}
    limiter = anyio.CapacityLimiter(CONCURRENCY)

    async def one(ident: str) -> None:
        async with limiter:
            try:
                found[ident] = await service.native_track(connection, ident, library_bound=True)
            except (PlaylistError, KeyError, ValueError):
                return  # unreadable, elsewhere or without a usable artist and title

    with anyio.move_on_after(SOUND_SECONDS):
        async with anyio.create_task_group() as group:
            for ident in ids:
                group.start_soon(one, ident)
    return [found[i] for i in ids if i in found]
