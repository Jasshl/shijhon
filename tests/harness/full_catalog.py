"""An invented full catalog, using the same recordings as the ordinary add-on fake."""

from __future__ import annotations

from typing import Any

from tests.harness.addon_catalog import Act, FakeCatalog, Record, Song

DECLARATION = {"key": "examplemusic", "region": "us", "clientArtwork": True}
RESOURCES = ("stream", "isrc", "search", "catalog", "fullcatalog")


class FullCatalog(FakeCatalog):
    def __init__(self, *, prefix: str = "") -> None:
        super().__init__(prefix=prefix)
        self.pages: dict[str, dict[str, Any]] = {}

    def credits(self, credit: str) -> list[dict[str, str]]:
        return [{"id": self.id(a.id), "name": a.name} for a in self.acts if a.name in credit]

    def song(self, song: Song, record: Record) -> dict[str, Any]:
        return super().song(song, record) | {
            "album": {"id": self.id(record.id), "title": record.title},
            "artists": self.credits(song.artist),
            "durationMs": (song.seconds or 1) * 1000 + 125,
            "discNumber": 2,
            "trackNumber": record.songs.index(song) + 1,
            "explicit": True,
            "releaseDate": record.date or str(record.year),
            "genres": ["Invented"],
            "artworkTemplate": f"{self.images}/{record.id}/{{w}}x{{h}}.jpg",
        }

    def listed(self, record: Record, *, artist: bool = True) -> dict[str, Any]:
        return super().listed(record, artist=artist) | {
            "artists": self.credits(record.artist),
            "kind": "ep" if len(record.songs) == 1 else "album",
            "explicit": True,
            "releaseDate": record.date or str(record.year),
            "label": "Imaginary Records",
            "upc": "000000000001",
            "genres": ["Invented"],
            "artworkTemplate": f"{self.images}/{record.id}/{{w}}x{{h}}.jpg",
        }

    def named(self, act: Act) -> dict[str, Any]:
        return super().named(act) | {"artworkTemplate": f"{self.images}/{act.id}/{{w}}x{{h}}.jpg"}

    def track(self, ident: str) -> dict[str, Any] | None:
        return next((self.song(s, r) for s, r in self._songs() if self.id(s.id) == ident), None)

    def isrc(self, code: str) -> dict[str, Any]:
        return {"tracks": [self.song(s, r) for s, r in self._songs() if s.isrc == code]}

    def releases(self, ident: str, cursor: str) -> dict[str, Any] | None:
        return self.pages.get(cursor)

    def catalog_request(self, path: str, cursor: str) -> dict[str, Any] | None:
        parts = path.split("/")
        if parts[1] == "track":
            return self.track(parts[2])
        if parts[1] == "isrc":
            return self.isrc(parts[2])
        return self.releases(parts[2], cursor)
