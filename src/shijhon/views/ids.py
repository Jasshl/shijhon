"""Catalog IDs as clients see them.

A catalog item that is not in the library has an ID of the form ``sh.<kind>.<catalog>.
<id>`` — kind ``al`` (album), ``tr`` (song) or ``ar`` (artist) — for example
``sh.al.demo.900000001``: ``<catalog>`` is the catalog's key (lower-case letters and
digits, ``catalog.model.CATALOG_KEY``) and ``<id>`` the catalog's own ID of the item
(letters, digits and dots, ``catalog.model.ITEM_ID``). An artist the catalog links to no
artist item has a reference through the song or album that credits it
(``catalog.model.item_artist``: ``t-<song>-<n>``, which is why this form also takes
hyphens). Navidrome's own IDs never contain a dot, so the two cannot be confused. Artwork
IDs follow Navidrome's naming (``al-``/``ar-``/``mf-`` + ID, optionally ``_<suffix>``),
because some clients construct them themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from shijhon.catalog.model import CatalogRef

Kind = Literal["al", "tr", "ar"]
_ID = re.compile(r"sh\.(al|tr|ar)\.([a-z][a-z0-9]*)\.([A-Za-z0-9.-]{1,200})")
_ART = re.compile(r"(?:al|ar|mf|pl)-(.+?)(?:_[0-9a-f]+)?")


@dataclass(frozen=True)
class CatalogId:
    kind: Kind
    ref: CatalogRef

    def __str__(self) -> str:
        return f"sh.{self.kind}.{self.ref.catalog}.{self.ref.id}"

    @classmethod
    def parse(cls, text: str | None) -> CatalogId | None:
        match = _ID.fullmatch(text or "")
        if match is None:
            return None
        kind: Kind = match.group(1)  # type: ignore[assignment]
        return cls(kind, CatalogRef(match.group(2), match.group(3)))

    @classmethod
    def parse_artwork(cls, text: str | None) -> CatalogId | None:
        """A catalog ID given as a cover-art ID, with or without Navidrome's prefix."""
        found = cls.parse(text)
        if found is not None:
            return found
        match = _ART.fullmatch(text or "")
        return cls.parse(match.group(1)) if match else None


def album_id(ref: CatalogRef) -> str:
    return str(CatalogId("al", ref))


def song_id(ref: CatalogRef) -> str:
    return str(CatalogId("tr", ref))


def artist_id(ref: CatalogRef) -> str:
    return str(CatalogId("ar", ref))
