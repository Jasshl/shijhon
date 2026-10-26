"""The catalog version of an import album below its importer's minimum quality
(``overlap.py``): looked for in the review with the existing album matching, added at
publication through the existing commit path.

**Looking**: the album's files as an owned album (their tags and lengths, without track
totals, so that a complete album is still matched to its release) go to
``Matcher.match`` with automatic picks; the release it settles on has every one of those
files when its links name them all. A catalog that does not answer within
``LOOK_SECONDS`` is ``CatalogUnreachable``.

**Adding**: ``Commits.native`` adds exactly the reviewed release (its fingerprint is
checked at the write boundary) - into the library's same-named album when there is one
(``fill/fills.first_use``) - under the import's own authorization, re-checked at every
write gate. The tracks' native song IDs follow. A refusal that will not change on its own
(the release would join an album it cannot, the catalog no longer has it or changed it)
is ``CatalogRefused``; anything else is left to a retry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

import anyio

from shijhon.catalog.base import CatalogError
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack
from shijhon.matching.albums import keys
from shijhon.matching.matcher import OwnedAlbum, OwnedSong
from shijhon.music_imports.overlap import (
    CatalogFound,
    CatalogRefused,
    CatalogUnreachable,
    Incoming,
)
from shijhon.placeholders.engine import MaterializeError
from shijhon.playlists.service import release_fingerprint
from shijhon.views.commits import CommitError
from shijhon.views.ids import CatalogId
from shijhon.views.virtual import NOT_FOUND

if TYPE_CHECKING:
    from shijhon.matching.matcher import Matcher
    from shijhon.views.commits import Commits

LOOK_SECONDS = 60.0  # one album's catalog matching at most
CHANGED = "the release differs from the saved selection"  # the engine's selection refusal
# Refusals of a commit that a retry does not change.
_REFUSED = frozenset(
    {"album_collision", "owned_ambiguous", "owned_incompatible", "owned_plan", "selection_changed"}
)


def owned_album(items: Sequence[Incoming]) -> OwnedAlbum:
    """The files of one import album as the album matching compares owned files."""
    first = items[0]
    years = [i.year for i in items if i.year]
    songs = []
    for item in items:
        song = item.song
        songs.append(
            OwnedSong(
                id=item.id,
                title=song.title,
                artist=song.artist,
                disc=song.disc or 1,
                number=song.number or 0,
                duration_ms=song.duration_ms,
                isrcs=song.isrcs,
                disc_tagged=song.disc is not None,
            )
        )
    # The files' own kind of a clean/explicit pair (their advisory tags or titles), so
    # that matching picks that edition rather than the global preference.
    kinds = {_clean(item) for item in items} - {None}
    return OwnedAlbum(
        id="import",
        title=first.album,
        artist=first.album_artist,
        year=min(years) if years else None,
        songs=tuple(songs),
        clean=kinds.pop() if len(kinds) == 1 else None,
    )


def _clean(item: Incoming) -> bool | None:
    """Whether the file is a clean edit (True), explicit (False) or does not say."""
    return keys(item.song).clean


def _other_recording(item: Incoming, track: CatalogTrack) -> bool:
    """A catalog track of the other recording of a clean/explicit pair than the file's."""
    clean = _clean(item)
    if clean is True:
        return not track.clean
    if clean is False:
        return track.clean
    return False


class CatalogRoute:
    def __init__(self, matcher: Matcher, commits: Commits, *, seconds: float = LOOK_SECONDS):
        self.matcher, self.commits, self.seconds = matcher, commits, seconds

    async def find(self, items: Sequence[Incoming]) -> CatalogFound | None:
        if not items or not items[0].album:
            return None
        album = owned_album(items)
        try:
            with anyio.fail_after(self.seconds):
                match = await self.matcher.match(album, pick=True)
        except CatalogError as exc:
            if exc.kind == "not_found":
                return None
            raise CatalogUnreachable from None
        except TimeoutError:
            raise CatalogUnreachable from None
        if match.release is None:
            return None
        by_ref = {track.ref: track for track in match.release.tracks}
        by_item = {item.id: item for item in items}
        if any(
            ref in by_ref and song in by_item and _other_recording(by_item[song], by_ref[ref])
            for ref, song in match.links.items()
        ):
            return None  # only the other recording of a clean/explicit pair: not this album
        tracks = {song: str(ref) for ref, song in match.links.items()}
        return CatalogFound(str(match.release.ref), release_fingerprint(match.release), tracks)

    async def add(
        self,
        release: str,
        tracks: Sequence[str],
        *,
        fingerprint: str,
        authorize: Callable[[], Awaitable[None]],
    ) -> dict[str, str]:
        """Catalog track -> native song ID, the reviewed release added to the library."""
        ref = CatalogRef.parse(release)

        def check(found: CatalogRelease) -> None:
            if release_fingerprint(found) != fingerprint:
                raise MaterializeError(CHANGED)

        engine = self.commits.engine
        try:
            with engine.guard_materializations(check), engine.guard_writes(authorize):
                await self.commits.native(CatalogId("al", ref), "import")
        except CommitError as exc:
            if exc.category in _REFUSED or exc.code == NOT_FOUND or CHANGED in exc.message:
                raise CatalogRefused(exc.message) from None
            raise
        found: dict[str, str] = {}
        for track in tracks:
            song = await self.commits.materialized.song(CatalogRef.parse(track))
            if song is not None:
                found[track] = song
        return found
