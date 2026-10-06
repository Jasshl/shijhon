"""The catalog artists clients were shown recently, by name.

``getTopSongs`` names an artist; a client asks for it when it shows an artist's page, with
the name that page showed. The catalog artists Shijhon added to a search result or
answered as an artist page are remembered here by their folded name - the most recent
last, the first of a name within one answer (its best match) - so that the name is that
artist, with no catalog request. Also each one's name by its ID (a request that gives the
ID). In memory: after a restart, a name is looked up again (a catalog search).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable

from shijhon.catalog.model import CatalogArtist, CatalogRef
from shijhon.matching.normalize import fold

SIZE = 5000  # artists remembered


class ShownArtists:
    def __init__(self, size: int = SIZE) -> None:
        self.size = size
        self._by_name: OrderedDict[str, CatalogRef] = OrderedDict()
        self._names: OrderedDict[CatalogRef, str] = OrderedDict()

    def shown(self, artists: Iterable[CatalogArtist]) -> None:
        """The catalog artists one answer showed, in its order."""
        seen: set[str] = set()
        for artist in artists:
            self._names[artist.ref] = artist.name
            self._names.move_to_end(artist.ref)
            key = fold(artist.name)
            if not key or key in seen:
                continue  # the first of a name in one answer is the one shown first
            seen.add(key)
            self._by_name[key] = artist.ref
            self._by_name.move_to_end(key)
        for kept in (self._by_name, self._names):
            while len(kept) > self.size:
                kept.popitem(last=False)

    def find(self, name: str) -> CatalogRef | None:
        """The catalog artist shown most recently with exactly this (folded) name."""
        key = fold(name)
        return self._by_name.get(key) if key else None

    def name_of(self, ref: CatalogRef) -> str | None:
        return self._names.get(ref)
