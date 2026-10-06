"""A made-up catalog: invented artists, albums and songs, answered from files.

The catalog of Shijhon's tests and development tools, as a catalog adapter: a package
of its own (``shijhon-demo-catalog``, in the development environment only) that registers
``[catalog] kind = "demo"``. Search results, artist pages, albums shown complete - with
data that does not exist: the names, IDs, ISRCs and dates are invented, its covers are plain
colored squares made here, and no add-on will find audio for its songs. The tests use its
records through ``tests/harness/replay.py``, with failures and delays injected around them.

The records (``records/*.json``) are answers in Shijhon's own catalog model
(``catalog.model``): ``{"name", "path", "status", "body"}``, a search's also ``"params":
{"term"}``. They keep the variants a real catalog has - editions and remasters, clean and
explicit twins, singles next to albums, an EP, "feat." and duo credits with diacritics,
soundtrack compilations, an artist without singles, search results that name no artist
items. ``path`` is the demo's name for a request:

- ``search`` - a search, by its term (the recorded terms are invented too; any other term
  finds the items whose names hold its words);
- ``albums/<id>``, ``songs/<id>``, ``artists/<id>`` - one item; every track of a recorded
  album is also a song;
- ``artists/<id>/releases`` - the artist's albums, singles and EPs;
- ``artists/<id>/top-songs`` - the artist's most popular songs;
- ``songs`` and ``albums`` - several items at once: songs by their ISRC, and the artist
  items of the songs and albums a search showed.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import replace
from importlib import resources
from typing import Any

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    artist_from_data,
    release_from_data,
    track_from_data,
)
from shijhon.catalog.plugin import Adapter, Context
from shijhon.matching.normalize import credit_names, fold

KEY = "demo"
LABEL = "Demo (invented data)"
NOTICE = (
    "The demo catalog is in use: its artists, albums and songs are invented. Nothing in it"
    " exists, and add-ons will not find audio for it."
)
ARTWORK = re.compile(r"https://covers\.demo\.invalid/([A-Za-z0-9.]{1,64})/(\d{1,4})x(\d{1,4})\.png")
MAX_COVER = 1200  # pixels: the largest cover made

Record = dict[str, Any]


def load_records() -> list[Record]:
    """The demo's records, in the order of their names (a fresh copy each time)."""
    folder = resources.files(__name__) / "records"
    files = sorted((f for f in folder.iterdir() if f.name.endswith(".json")), key=lambda f: f.name)
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def results_from_data(body: dict[str, Any]) -> SearchResults:
    return SearchResults(
        artists=tuple(artist_from_data(a) for a in body.get("artists") or ()),
        albums=tuple(release_from_data(a) for a in body.get("albums") or ()),
        songs=tuple(track_from_data(s) for s in body.get("songs") or ()),
    )


def cover(seed: str, size: int) -> bytes:
    """A plain square of a color made from ``seed``, as a PNG of ``size`` pixels."""
    size = max(1, min(size, MAX_COVER))
    digest = hashlib.sha256(seed.encode()).digest()
    color = bytes(64 + value // 2 for value in digest[:3])  # mid tones
    rows = (b"\x00" + color * size) * size

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


class DemoCatalog:
    """The records behind Shijhon's catalog interface. ``by_path`` and ``searches`` are
    read on every call (a test changes a record, and the next answer has it).

    ``names`` (the demo): a search for a term that is not a recorded one finds items by
    their names, and lists hold only what can be opened - albums with their tracks recorded,
    their songs, artists with a record of their own. Off (tests): exactly the recorded
    answers, and a term nobody recorded is not found."""

    key = KEY

    def __init__(
        self, records: Iterable[Record] | None = None, *, region: str = "", names: bool = True
    ) -> None:
        records = load_records() if records is None else list(records)
        self.region = region
        self.names = names
        self.by_path: dict[str, Record] = {r["path"]: r for r in records if r["path"] != "search"}
        self.searches: dict[str, Record] = {
            str(r["params"]["term"]).lower(): r for r in records if r["path"] == "search"
        }

    async def aclose(self) -> None:
        return None

    # --- hooks (the tests' replay counts, delays and fails requests here) -----------------

    async def asked(self, key: str, entry: str) -> None:
        """A request is about to be answered: ``key`` names it without its arguments (as in
        the module's list above, e.g. ``albums/<id>`` or ``search``), ``entry`` with them."""

    async def artwork_asked(self, url: str) -> None:
        """A cover is about to be made."""

    def search_record(self, term: str) -> Record | None:
        """The recorded search answering ``term``."""
        return self.searches.get(term.lower())

    # --- the records ----------------------------------------------------------------------

    def _body(self, path: str) -> Any:
        """A record's body; a record with another status than 200 is that failure."""
        record = self.by_path.get(path)
        if record is None:
            raise CatalogError("not_found", "not in the catalog")
        if record.get("status", 200) != 200:
            raise failure(int(record["status"]))
        return record["body"]

    def _records(self) -> list[tuple[str, Record]]:
        """The records as they are now (a copy of the list: a test may add or drop one
        from another thread while an answer is made)."""
        return list(self.by_path.items())

    def _album_bodies(self) -> Iterator[dict[str, Any]]:
        """The recorded albums, as their records hold them (read as data: answers are made
        on the event loop, and only what is asked for is turned into the model)."""
        for path, record in self._records():
            if path.startswith("albums/") and record.get("status", 200) == 200:
                yield record["body"]

    def _raw_songs(self) -> Iterator[tuple[dict[str, Any], list[dict[str, Any]] | None]]:
        """Every song as data, with the artist items of its album when it is an album's
        track (None: a recorded song, with its own): the recorded ones, then the recorded
        albums' tracks."""
        seen = set()
        for path, record in self._records():
            if path.startswith("songs/") and record.get("status", 200) == 200:
                seen.add(path.split("/", 1)[1])
                yield record["body"], None
        for body in self._album_bodies():
            for raw in body.get("tracks") or ():
                song_id = raw["ref"]["id"]
                if song_id not in seen:
                    seen.add(song_id)
                    yield raw, body.get("artist_refs") or []

    @staticmethod
    def _track(raw: dict[str, Any], album_artists: list[dict[str, Any]] | None) -> CatalogTrack:
        track = track_from_data(raw)
        if album_artists is None:
            return track
        refs = tuple(CatalogRef(str(r["catalog"]), str(r["id"])) for r in album_artists)
        return replace(track, artist_refs=refs)

    def _song(self, song_id: str) -> CatalogTrack | None:
        """A recorded song, else the track of a recorded album (with the album's artists)."""
        for raw, album_artists in self._raw_songs():
            if raw["ref"]["id"] == song_id:
                return self._track(raw, album_artists)
        return None

    def _songs(self) -> Iterator[CatalogTrack]:
        """Every song: the recorded ones, then the recorded albums' tracks."""
        for raw, album_artists in self._raw_songs():
            yield self._track(raw, album_artists)

    # --- the catalog interface ------------------------------------------------------------

    async def check(self) -> None:
        await self.asked("check", "check")

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        await self.asked("search", f"search?term={term}")
        record = self.search_record(term)
        if record is None:
            if not self.names:
                raise CatalogError("not_found", "not in the catalog")
            found = self._named(term)
        elif record.get("status", 200) != 200:
            raise failure(int(record["status"]))
        else:
            found = results_from_data(record["body"])
        if self.names:
            found = SearchResults(
                tuple(a for a in found.artists if self._opens(a.ref.id)),
                self._opening(found.albums),
                self._of_opening(found.songs),
            )
        limit = max(1, limit)
        return SearchResults(found.artists[:limit], found.albums[:limit], found.songs[:limit])

    def _opens(self, artist_id: str) -> bool:
        """Whether the demo can open this artist: a record of its own, or an album's credit."""
        return f"artists/{artist_id}" in self.by_path or self._credited(artist_id)[0] is not None

    def _opening(self, releases: Iterable[CatalogRelease]) -> tuple[CatalogRelease, ...]:
        """The releases whose tracks are recorded (the demo lists nothing that cannot be
        opened)."""
        return tuple(r for r in releases if f"albums/{r.ref.id}" in self.by_path)

    def _of_opening(self, songs: Iterable[CatalogTrack]) -> tuple[CatalogTrack, ...]:
        return tuple(
            s for s in songs if s.album is not None and f"albums/{s.album.id}" in self.by_path
        )

    def _named(self, term: str) -> SearchResults:
        """The items whose names hold every word of ``term`` (an artist's: the name; an
        album's or a song's: its title and artist)."""
        words = [fold(word) for word in term.split() if fold(word)]

        def holds(*names: str | None) -> bool:
            text = fold(" ".join(name or "" for name in names))
            return bool(words) and all(word in text for word in words)

        artists: dict[str, CatalogArtist] = {}
        albums: dict[str, CatalogRelease] = {}
        songs: dict[str, CatalogTrack] = {}
        for path, record in self._records():
            if record.get("status", 200) != 200:
                continue
            body = record["body"]
            if path.startswith("artists/") and path.count("/") == 1:
                artist = artist_from_data(body)
                artists.setdefault(artist.ref.id, artist)
            elif path.endswith("/releases"):
                for release in map(release_from_data, body):
                    albums.setdefault(release.ref.id, release)
        for body in self._album_bodies():  # search results list no tracks
            album = release_from_data({**body, "tracks": []})
            albums[album.ref.id] = album
        for song in self._songs():
            songs.setdefault(song.ref.id, song)
        return SearchResults(
            artists=tuple(a for a in artists.values() if holds(a.name)),
            albums=tuple(a for a in albums.values() if holds(a.title, a.artist)),
            songs=tuple(s for s in songs.values() if holds(s.title, s.artist)),
        )

    async def album(self, album_id: str) -> CatalogRelease:
        await self.asked(f"albums/{album_id}", f"albums/{album_id}")
        return release_from_data(self._body(f"albums/{album_id}"))

    async def song(self, song_id: str) -> CatalogTrack:
        await self.asked(f"songs/{song_id}", f"songs/{song_id}")
        record = self.by_path.get(f"songs/{song_id}")
        if record is not None and record.get("status", 200) != 200:
            raise failure(int(record["status"]))
        found = self._song(song_id)
        if found is None:
            raise CatalogError("not_found", "not in the catalog")
        return found

    async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
        await self.asked("songs", f"songs?isrc={isrc}")
        wanted = isrc.upper()
        return tuple(
            self._track(raw, album_artists)
            for raw, album_artists in self._raw_songs()
            if str(raw.get("isrc") or "").upper() == wanted
        )

    def _credited(self, artist_id: str) -> tuple[str | None, tuple[CatalogRelease, ...]]:
        """An artist item by what names it: its name as the credit has it (its part of a
        credit that lists as many names as artist items, else the whole credit), and the
        recorded albums that name it - or hold a recorded song that does."""
        name: str | None = None
        releases: dict[str, CatalogRelease] = {}
        albums = {body["ref"]["id"]: body for body in self._album_bodies()}

        def credit(text: str, ids: list[str]) -> None:
            nonlocal name
            if name is None:
                names = credit_names(text)
                name = names[ids.index(artist_id)] if len(names) == len(ids) else text

        def listed(body: dict[str, Any]) -> None:
            if body["ref"]["id"] not in releases:
                releases[body["ref"]["id"]] = release_from_data({**body, "tracks": []})

        def named_by(item: dict[str, Any]) -> list[str] | None:
            """The item's artist IDs when this artist is among them (read once: a record
            may be changed from another thread)."""
            ids = [str(ref["id"]) for ref in list(item.get("artist_refs") or ())]
            return ids if artist_id in ids else None

        for body in albums.values():
            ids = named_by(body)
            if ids is not None:
                credit(str(body.get("artist") or ""), ids)
                listed(body)
        for path, record in self._records():
            if not path.startswith("songs/") or record.get("status", 200) != 200:
                continue
            song = record["body"]
            ids = named_by(song)
            if ids is not None:
                credit(str(song.get("artist") or ""), ids)
                of = albums.get((song.get("album") or {}).get("id"))
                if of is not None:
                    listed(of)
        return name, tuple(releases.values())

    async def artist(self, artist_id: str) -> CatalogArtist:
        await self.asked(f"artists/{artist_id}", f"artists/{artist_id}")
        if self.names and f"artists/{artist_id}" not in self.by_path:
            # The demo opens every artist its albums and songs name: one without a record
            # of its own is known by their credit.
            name, _ = self._credited(artist_id)
            if name is not None:
                return CatalogArtist(CatalogRef(self.key, artist_id), name)
        return artist_from_data(self._body(f"artists/{artist_id}"))

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        path = f"artists/{artist_id}/releases"
        await self.asked(path, path)
        if path not in self.by_path:
            # An artist without releases here (the demo: the albums that credit it).
            return self._credited(artist_id)[1] if self.names else ()
        releases = tuple(release_from_data(r) for r in self._body(path))
        return self._opening(releases) if self.names else releases

    async def top_songs(self, artist_id: str, limit: int = 10) -> tuple[CatalogTrack, ...]:
        path = f"artists/{artist_id}/top-songs"
        await self.asked(path, path)
        if path not in self.by_path:
            return ()  # an artist without any
        songs = tuple(track_from_data(s) for s in self._body(path))
        return (self._of_opening(songs) if self.names else songs)[: max(0, limit)]

    async def artists_of(
        self, songs: tuple[str, ...], albums: tuple[str, ...]
    ) -> dict[str, tuple[CatalogRef, ...]]:
        """The demo's search results name no artist items (as real ones may not): the songs'
        and albums' own records do. One request for each kind."""
        found: dict[str, tuple[CatalogRef, ...]] = {}
        if songs:
            await self.asked("songs", "songs")
            for song_id in songs:
                song = self._song(song_id)
                if song is not None and song.artist_refs:
                    found[f"songs:{song_id}"] = song.artist_refs
        if albums:
            await self.asked("albums", "albums")
            for album_id in albums:
                record = self.by_path.get(f"albums/{album_id}")
                if record is None or record.get("status", 200) != 200:
                    continue
                refs = tuple(
                    CatalogRef(str(r["catalog"]), str(r["id"]))
                    for r in record["body"].get("artist_refs") or ()
                )
                if refs:
                    found[f"albums:{album_id}"] = refs
        return found

    async def artwork(self, url: str) -> tuple[bytes, str]:
        """The cover of a demo address: a colored square made here (nothing is fetched)."""
        match = ARTWORK.fullmatch(url)
        if match is None:
            raise CatalogError("invalid", "not a catalog artwork URL")
        await self.artwork_asked(url)
        return cover(match.group(1), int(match.group(2))), "image/png"


def failure(status: int) -> CatalogError:
    """A request's failure by its status, as an HTTP catalog would report it."""
    if status == 404:
        return CatalogError("not_found", "not in the catalog")
    if status in (401, 403):
        return CatalogError("unauthorized", f"HTTP {status}")
    if status == 429:
        return CatalogError("rate_limited", "HTTP 429")
    return CatalogError("unavailable", f"HTTP {status}")


def build(settings: Any, context: Context) -> DemoCatalog:
    return DemoCatalog()


adapter = Adapter(label=LABEL, build=build, notice=NOTICE)
