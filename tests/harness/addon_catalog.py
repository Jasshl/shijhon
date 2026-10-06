"""An invented catalog for the fake add-on: what an add-on with a catalog
answers to ``/search``, ``/album/{id}`` and ``/artist/{id}``, in the shapes real add-ons
answer with. Every artist, title, ID and ISRC is made up.

``shape`` picks one of the variations seen among add-ons:

- ``plain`` - the fields as the protocol documents them;
- ``renamed`` - an album answer with ``releaseDate`` and ``totalTracks`` and without
  ``id`` and ``year``; an artist answer with ``tracks`` in place of ``topTracks`` and
  without ``id``; albums in lists without ``trackCount`` and with the year as text;
- ``rich`` - more than is read: ``name``, ``cover``, ``poster``, ``background``,
  ``description``, ``format``, ``infotext``, ``isHiRes``, ``bio``, ``genres``, ``images``,
  playlists in a search; years as text.

``prefix`` goes before every ID ("lib:" makes IDs that need translating). A test can
replace any answer (``answers``: path -> the JSON to send), delay or fail a request
(``delay``, ``status``: by its endpoint), or change the songs and albums themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

IMAGES = "https://images.fake.test"


@dataclass
class Song:
    id: str
    title: str
    artist: str
    seconds: int | None = 4
    isrc: str | None = None


@dataclass
class Record:
    """An album of the invented catalog."""

    id: str
    title: str
    artist: str
    year: int
    songs: list[Song]
    date: str | None = None  # its day, where the shape sends one


@dataclass
class Act:
    """An artist: ``top`` are song IDs, the most popular first."""

    id: str
    name: str
    top: list[str] = field(default_factory=list)


def library() -> tuple[list[Record], list[Act]]:
    """The invented catalog: artists with a feature and a duo credit between them, a
    single, a song without an ISRC, two albums of one title by different artists - and a
    third artist's album and single that the suites add to the library (the others stay
    out of it: the views are compared whatever was added before)."""
    venn, brandt, calder = "Mara Venn", "Odile Brandt", "Ines Calder"
    records = [
        Record(
            "al1",
            "Glass Rivers",
            venn,
            2019,
            [
                Song("t101", "Glass Rivers", venn, 4, "ZZSHA0000101"),
                Song("t102", "Salt Meadow", venn, 5, "ZZSHA0000102"),
                Song("t103", "Harbor Lights", f"{venn} feat. {brandt}", 6, "ZZSHA0000103"),
                Song("t104", "Slow Thaw", venn, 7),  # no ISRC
            ],
            "2019-03-08",
        ),
        Record(
            "al2",
            "Low Tide - Single",
            venn,
            2020,
            [Song("t201", "Low Tide", venn, 4, "ZZSHA0000201")],
            "2020-06-19",
        ),
        Record(
            "al3",
            "North Window",
            brandt,
            2021,
            [
                Song("t301", "North Window", brandt, 5, "ZZSHA0000301"),
                Song("t302", "Two Kettles", f"{brandt}, {venn}", 6, "ZZSHA0000302"),
                Song("t303", "Ash and Elm", brandt, 4, "ZZSHA0000303"),
            ],
            "2021-11-05",
        ),
        # The same title by another artist: a song's album is found by title and artist.
        Record(
            "al4",
            "North Window",
            "Tobin Aske",
            2015,
            [Song("t401", "Northern Rooms", "Tobin Aske", 5, "ZZSHA0000401")],
        ),
        Record(
            "al5",
            "Paper Lanterns",
            calder,
            2022,
            [
                Song("t501", "Paper Lanterns", calder, 4, "ZZSHA0000501"),
                Song("t502", "Kite Season", calder, 5, "ZZSHA0000502"),
                Song("t503", "Wax and Wire", calder, 4, "ZZSHA0000503"),
            ],
        ),
        Record(
            "al6",
            "Tin Roof - Single",
            calder,
            2023,
            [Song("t601", "Tin Roof", calder, 4, "ZZSHA0000601")],
        ),
        Record(
            "al12",
            "Harbor Arithmetic",
            "Selma Roe",
            2021,
            [
                Song("t1201", "First Ferry", "Selma Roe", 4, "ZZSHA0001201"),
                Song("t1202", "Counting Gulls", "Selma Roe", 5, "ZZSHA0001202"),
                Song("t1203", "Last Ferry", "Selma Roe", 4, "ZZSHA0001203"),
            ],
        ),
    ]
    acts = [
        Act("ar1", venn, ["t102", "t101", "t201", "t103"]),
        Act("ar2", brandt, ["t301", "t303"]),
        Act("ar3", "Tobin Aske", []),
        Act("ar4", calder, ["t502"]),
    ]
    return records, acts


class FakeCatalog:
    def __init__(self, shape: str = "plain", *, prefix: str = "") -> None:
        assert shape in ("plain", "renamed", "rich")
        self.shape = shape
        self.prefix = prefix
        self.records, self.acts = library()
        self.answers: dict[str, Any] = {}  # "search", "album/<id>", "artist/<id>" -> JSON
        self.delay: dict[str, float] = {}  # "search" | "album" | "artist" -> seconds
        self.status: dict[str, int] = {}  # ... -> the HTTP status to answer with
        self.limit = 20  # items of each kind in a search answer
        self.images = IMAGES  # where its covers are (an image host; a test may move them)

    # --- the items as the add-on names them ---------------------------------------------

    def id(self, own: str) -> str:
        return f"{self.prefix}{own}"

    def _art(self, seed: str) -> str:
        return f"{self.images}/{seed}/640x640.jpg"

    def song(self, song: Song, record: Record) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": self.id(song.id),
            "title": song.title,
            "artist": song.artist,
            "album": record.title,
            "artworkURL": self._art(record.id),
        }
        if song.seconds is not None:
            item["duration"] = song.seconds
        if song.isrc is not None:
            item["isrc"] = song.isrc
        if self.shape == "rich":
            item |= {"format": "flac", "infotext": None, "isHiRes": False}
        return item

    def listed(self, record: Record, *, artist: bool = True) -> dict[str, Any]:
        """An album as lists have it (a search, an artist's albums)."""
        item: dict[str, Any] = {
            "id": self.id(record.id),
            "title": record.title,
            "artworkURL": self._art(record.id),
            "year": record.year if self.shape == "plain" else str(record.year),
        }
        if artist:
            item["artist"] = record.artist
        if self.shape != "renamed":
            item["trackCount"] = len(record.songs)
        if self.shape == "rich":
            art = self._art(record.id)
            item |= {"name": record.title, "cover": art, "poster": art, "background": art}
        return item

    def named(self, act: Act) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": self.id(act.id),
            "name": act.name,
            "artworkURL": self._art(act.id),
        }
        if self.shape == "rich":
            item |= {"title": act.name, "genres": ["Invented"], "images": [self._art(act.id)]}
        return item

    # --- the answers ----------------------------------------------------------------------

    def _songs(self) -> list[tuple[Song, Record]]:
        return [(song, record) for record in self.records for song in record.songs]

    def search(self, term: str) -> dict[str, Any]:
        """The items whose names hold every word of ``term``."""
        words = term.lower().split()

        def holds(*names: str) -> bool:
            line = " ".join(names).lower()
            return bool(words) and all(word in line for word in words)

        answer: dict[str, Any] = {
            "tracks": [
                self.song(song, record)
                for song, record in self._songs()
                if holds(song.title, song.artist, record.title)
            ][: self.limit],
            "albums": [self.listed(r) for r in self.records if holds(r.title, r.artist)][
                : self.limit
            ],
            "artists": [self.named(a) for a in self.acts if holds(a.name)][: self.limit],
        }
        if self.shape == "rich":
            answer["playlists"] = [{"id": "pl1", "title": "Made up", "trackCount": 3}]
        return answer

    def album(self, ident: str) -> dict[str, Any] | None:
        record = next((r for r in self.records if self.id(r.id) == ident), None)
        if record is None:
            return None
        tracks = [self.song(song, record) for song in record.songs]
        if self.shape == "renamed":
            return {
                "tracks": tracks,
                "title": record.title,
                "artist": record.artist,
                "releaseDate": record.date or str(record.year),
                "artworkURL": self._art(record.id),
                "totalTracks": len(tracks),
            }
        answer = self.listed(record) | {"trackCount": len(tracks), "tracks": tracks}
        if self.shape == "rich":
            answer["description"] = "An invented album."
        return answer

    def artist(self, ident: str) -> dict[str, Any] | None:
        act = next((a for a in self.acts if self.id(a.id) == ident), None)
        if act is None:
            return None
        by_id = {song.id: (song, record) for song, record in self._songs()}
        top = [self.song(*by_id[song_id]) for song_id in act.top if song_id in by_id]
        # (As seen: an artist's own albums often name no artist.)
        albums = [self.listed(r, artist=False) for r in self.records if r.artist == act.name]
        answer = self.named(act)
        if self.shape == "renamed":
            del answer["id"]
            return answer | {"tracks": top, "albums": albums}
        if self.shape == "rich":
            answer["bio"] = "An invented artist."
        return answer | {"topTracks": top, "albums": albums}
