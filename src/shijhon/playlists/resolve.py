"""Conservative recording resolution: ambiguous versions remain unresolved, unless the
caller asks for the original (``prefer_original``: an agent's explicit tracks)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from shijhon.catalog.base import Catalog, CatalogError
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack
from shijhon.matching.normalize import (
    artist_names,
    core_title,
    edition_marked,
    featured_names,
    fold,
    lead_credit,
    recording_edition_marked,
    recording_key,
    version_marker,
)
from shijhon.playlists.client import OwnerClient
from shijhon.playlists.model import Connection, Track, is_text
from shijhon.views.ids import CatalogId, song_id

if TYPE_CHECKING:
    from shijhon.catalog.addon import AddonCatalogs


# Catalog failures that mean an add-on did not answer, not that it lacks a recording.
UNANSWERED = frozenset({"unavailable", "rate_limited"})


def same_recording(wanted: dict[str, Any], found: Track) -> bool:
    if wanted.get("required_title") and fold(wanted["required_title"]) != fold(found.title):
        return False
    if wanted.get("required_album") and fold(wanted["required_album"]) != fold(found.album):
        return False
    if fold(wanted["artist"]) != fold(found.artist) and not (
        wanted.get("lenient_credit") and similar_credit(wanted["artist"], found)
    ):
        return False
    if recording_key(wanted["title"]) != recording_key(found.title):
        return False
    marker = version_marker(wanted["title"])
    if wanted.get("clean") and not found.clean:
        return False
    if wanted.get("explicit") and not found.explicit:
        return False
    if marker == "clean" and not (found.clean or version_marker(found.title) == "clean"):
        return False
    if marker == "explicit" and not (found.explicit or version_marker(found.title) == "explicit"):
        return False
    if wanted.get("isrc") and found.isrc and wanted["isrc"] != found.isrc:
        return False
    duration = wanted.get("duration", 0)
    return not duration or not found.duration or abs(duration - found.duration) <= 5


def originals(matches: list[Track], artist: str = "") -> list[Track]:
    """Versions of one recording, the original first: credited as ``artist`` asks, not a
    clean edit, a title and an album without edition markers (remastered, deluxe), the
    earliest known year; then in the order the search gave them."""

    def rank(item: tuple[int, Track]) -> tuple[bool, bool, bool, bool, bool, int, int]:
        position, song = item
        return (
            bool(artist) and fold(song.artist) != fold(artist),
            song.clean,
            recording_edition_marked(song.title),
            edition_marked(song.album),
            not song.year,
            song.year,
            position,
        )

    return [song for _, song in sorted(enumerate(matches), key=rank)]


def matching(wanted: dict[str, Any], options: list[Track]) -> list[Track]:
    matches = [s for s in options if same_recording(wanted, s)]
    album = wanted.get("album", "")
    if album and wanted.get("strict_album"):
        matches = [s for s in matches if fold(s.album) == fold(album)]
    if album and (exact := [s for s in matches if fold(s.album) == fold(album)]):
        matches = exact
    return matches


def similar_credit(wanted: str, found: Track) -> bool:
    """An agent's explicit track: the same lead credit, featured artists aside, though the
    featured artists the caller names must be credited (in the credit or the title)."""
    if lead_credit(wanted) != lead_credit(found.artist):
        return False
    credited = artist_names(found.artist)
    title = " ".join(fold(w) for w in found.title.split())
    return all(
        fold(name) in credited
        or re.search(rf"(?<!\S){re.escape(lead_credit(name))}(?!\S)", title) is not None
        for name in featured_names(wanted)[1:]
    )


def choose(wanted: dict[str, Any], options: list[Track]) -> Track | None:
    """The wanted recording among ``options``; None when there is none or several
    uncorroborated ones (``prefer_original``: then the original of them)."""
    matches = matching(wanted, options)
    if len(matches) == 1:
        return matches[0]
    if matches and wanted.get("prefer_original"):
        return originals(matches, wanted["artist"])[0]
    # The same corroborated recording on several releases can be reused. Unknown ISRCs
    # or clean/explicit differences never become interchangeable through title alone.
    groups = {(s.isrc, s.explicit, s.clean) for s in matches}
    if matches and all(s.isrc for s in matches) and len(groups) == 1:
        return sorted(matches, key=lambda s: (bool(s.release), s.album, s.id))[0]
    return None


def genre_names(values: Any) -> list[str]:
    """A few distinct, short, printable genre names: hints, never identity."""
    names: dict[str, str] = {}
    for value in values if isinstance(values, (list, tuple)) else [values]:
        name = value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str) and (name := name.strip()) and len(name) <= 60 and is_text(name):
            names.setdefault(name.casefold(), name)
    return list(names.values())[:5]


def year_of(value: Any) -> int:
    try:
        year = int(str(value)[:4]) if value else 0
    except ValueError:
        return 0
    return year if 0 < year <= 9999 else 0


def native_track(data: dict[str, Any]) -> Track:
    isrc = data.get("isrc", "")
    if isinstance(isrc, list):
        isrc = isrc[0] if len(isrc) == 1 else ""
    return Track(
        id=str(data["id"]),
        artist=str(data.get("artist", "")),
        title=str(data.get("title", "")),
        album=str(data.get("album", "")),
        duration=int(data.get("duration", 0)),
        isrc=str(isrc or ""),
        explicit=data.get("explicitStatus") == "explicit",
        clean=data.get("explicitStatus") == "clean",
        year=year_of(data.get("year")),
        genres=genre_names(data.get("genres") or data.get("genre") or []),
    )


async def listed(
    catalog: Catalog, song: CatalogRef, release: str
) -> tuple[CatalogTrack, CatalogRelease] | None:
    """A song of a catalog without a request for one song (an add-on's), as its release
    lists it now: ``release`` (a reference), else the album the catalog finds for the
    song (``album_of``). None: no release lists it."""
    found: CatalogRef | None
    if release:
        try:
            found = CatalogRef.parse(release)
        except ValueError:
            return None
    else:
        lookup = getattr(catalog, "album_of", None)
        found = await lookup(song.id) if lookup is not None else None
    if found is None or found.catalog != song.catalog:
        return None
    album = await catalog.album(found.id)
    track = next((t for t in album.tracks if t.ref == song), None)
    if track is None:
        return None
    # (Asked for by its ID when it is added: known then, also from a cached answer.)
    remember = getattr(catalog, "remember", None)
    if remember is not None:
        remember([track])
    return track, album


def searched(songs: Any, *, by_release: bool) -> list[Track]:
    """A catalog search's songs as tracks; only those with a release unless
    ``by_release`` (an add-on's catalog, which finds the release later)."""
    return [
        Track(
            id=song_id(t.ref),
            artist=t.artist,
            title=t.title,
            duration=t.seconds,
            album=t.album_title or "",
            isrc=t.isrc or "",
            explicit=t.explicit,
            clean=t.clean,
            release=str(t.album) if t.album else "",
            year=year_of(t.release_date),
        )
        for t in songs
        if t.album or by_release
    ]


_ARTICLES = frozenset({"the", "a", "an"})


def _bare(title: str) -> str:
    """A title's core without a leading article, folded: "The Great Waltz" -> "greatwaltz"."""
    words = core_title(title).split()
    if len(words) > 1 and words[0].casefold() in _ARTICLES:
        words = words[1:]
    return fold(" ".join(words))


def _contains(a: str, b: str) -> bool:
    short, long = sorted((a, b), key=len)
    return len(short) >= 4 and short in long


def near_title(wanted: str, found: str) -> bool:
    """A title the caller may have meant: the same but for a leading article or
    qualifiers, or one containing the other ("Theme" and "Theme from Halls")."""
    return _contains(_bare(wanted), _bare(found))


def near_artist(wanted: str, found: str) -> bool:
    """A credit sharing a name with the wanted one, or containing it ("A" and "A Trio")."""
    return bool(artist_names(wanted) & artist_names(found)) or _contains(fold(wanted), fold(found))


Reason = Literal[
    "not_found",
    "album_mismatch",
    "version_mismatch",
    "artist_mismatch",
    "title_mismatch",
    "unconfirmed",
    "catalog_unavailable",
]
CANDIDATES = 5


@dataclass
class Explanation:
    """Why a wanted recording was not resolved, and the nearest songs found instead."""

    reason: Reason
    songs: list[Track] = field(default_factory=list)

    def candidates(self) -> list[dict[str, str]]:
        """A few distinct songs, as music names only."""
        shown: dict[tuple[str, str, str], dict[str, str]] = {}
        for song in self.songs:
            key = (fold(song.artist), fold(song.title), fold(song.album))
            shown.setdefault(key, {"artist": song.artist, "title": song.title, "album": song.album})
        return list(shown.values())[:CANDIDATES]


class Resolver:
    """``addons``: the catalogs of the add-ons, which imports alone ask."""

    def __init__(
        self, client: OwnerClient, catalog: Catalog | None, addons: AddonCatalogs | None = None
    ) -> None:
        self.client, self.catalog, self.addons = client, catalog, addons

    async def resolve(
        self,
        connection: Connection,
        wanted: dict[str, Any],
        *,
        ambiguous: list[Track] | None = None,
        addons: bool = False,
        unanswered: set[str] | None = None,
        unreadable: set[str] | None = None,
        failures: list[str] | None = None,
    ) -> Track | None:
        """The library's recording, else the catalog's. ``failures``: the kinds of the
        catalog's errors (``CatalogError.kind``) while it was asked. ``addons`` (imports, with
        ``ambiguous``): then, for a recording neither has - not even as several
        candidates -, the catalogs of the add-ons in their order: the first one with the
        recording, or with several candidates, decides. An add-on that is unavailable or
        rate limited is added to ``unanswered`` and not asked again while it is in it; one
        whose manifest is refused or broken goes to ``unreadable``, likewise."""
        owned = await self._owned(connection, wanted)
        if wanted.get("lenient_credit") and not any(same_recording(wanted, o) for o in owned):
            # A credit written otherwise ("and" for "&") is found by its title alone.
            owned += await self._owned(connection, wanted, core_title(wanted["title"]))
        found = choose(wanted, owned)
        if found:
            return found.model_copy(update={"score": wanted["score"], "reason": wanted["reason"]})
        if ambiguous is not None:
            ambiguous.extend(s for s in owned if same_recording(wanted, s))
            if ambiguous:
                return None
        if self.catalog is not None:
            found = await self._from(self.catalog, wanted, ambiguous, failures=failures)
            if found or ambiguous:
                return found
        if not addons or ambiguous is None or self.addons is None:
            return None
        skipped = unanswered if unanswered is not None else set()
        for name, catalog in await self.addons.catalogs(skipped, unreadable):
            if name in skipped:
                continue
            try:
                found = await self._from(catalog, wanted, ambiguous, by_release=True)
            except CatalogError:
                skipped.add(name)  # not "not found": the add-on did not answer
                continue
            if found or ambiguous:
                return found
        return None

    async def _owned(
        self, connection: Connection, wanted: dict[str, Any], query: str = ""
    ) -> list[Track]:
        result = await self.client.call(
            connection,
            "search3",
            [
                ("query", query or f"{wanted['artist']} {wanted['title']}"),
                ("songCount", "50"),
                ("albumCount", "0"),
                ("artistCount", "0"),
                ("musicFolderId", str(self.client.native.library_id)),
            ],
        )
        return [native_track(s) for s in result.get("searchResult3", {}).get("song", [])]

    async def explain(
        self,
        connection: Connection,
        wanted: dict[str, Any],
        *,
        library: bool = True,
        unanswered: bool = False,
    ) -> Explanation:
        """Why ``resolve`` found no recording for ``wanted``, with the nearest songs the
        catalog and - with ``library`` - the library show (a second look: only for what
        was not resolved). ``unanswered``: the catalog did not answer ``resolve`` (it failed
        otherwise than "not found"); such a failure here is one too."""
        seen = await self._owned(connection, wanted) if library else []
        if self.catalog is not None:
            try:
                results = await self.catalog.search(
                    f"{wanted['artist']} {wanted['title']}", limit=50
                )
            except CatalogError as exc:
                unanswered = unanswered or exc.kind != "not_found"
            else:
                seen += searched(results.songs, by_release=False)
        core = fold(core_title(wanted["title"]))
        any_album = {k: v for k, v in wanted.items() if k not in ("album", "required_album")}
        on_other = [s for s in seen if same_recording(any_album, s)]
        same_artist = [s for s in seen if lead_credit(s.artist) == lead_credit(wanted["artist"])]
        versions = [s for s in same_artist if fold(core_title(s.title)) == core]
        credited = [s for s in seen if near_artist(wanted["artist"], s.artist)]
        titled = [s for s in credited if fold(core_title(s.title)) == core]
        similar = [s for s in credited if near_title(wanted["title"], s.title)]
        if unanswered:
            return Explanation("catalog_unavailable", on_other or versions or titled or similar)
        if wanted.get("album") and on_other:
            return Explanation("album_mismatch", on_other)
        if on_other:  # listed, but its release could not be confirmed
            return Explanation("unconfirmed", on_other)
        if versions:
            return Explanation("version_mismatch", versions)
        if titled:
            return Explanation("artist_mismatch", titled)
        if similar:
            return Explanation("title_mismatch", similar)
        return Explanation("not_found", [])

    async def _from(
        self,
        catalog: Catalog,
        wanted: dict[str, Any],
        ambiguous: list[Track] | None,
        *,
        by_release: bool = False,
        failures: list[str] | None = None,
    ) -> Track | None:
        """The recording in ``catalog``. ``by_release``: a catalog without a request for
        one song (an add-on's): its songs as their releases list them (``listed``), also
        the songs of a search that names no release for them; such a catalog that is
        unavailable or rate limited raises its ``CatalogError`` (it did not answer).
        ``failures``: see ``resolve``."""
        try:
            results = await catalog.search(f"{wanted['artist']} {wanted['title']}", limit=50)
        except CatalogError as exc:
            if by_release and exc.kind in UNANSWERED:
                raise
            if failures is not None:
                failures.append(exc.kind)
            return None
        tracks = searched(results.songs, by_release=by_release)
        found = choose(wanted, tracks)
        if not found:
            if ambiguous is not None:
                ambiguous.extend(s for s in tracks if same_recording(wanted, s))
            return None
        if not wanted.get("prefer_original"):
            return await self._detailed(
                catalog, wanted, found, by_release=by_release, failures=failures
            )
        # The original first; a version whose release cannot be confirmed gives way to the
        # next (a few at most).
        for option in originals(matching(wanted, tracks), wanted["artist"])[:3]:
            detailed = await self._detailed(
                catalog, wanted, option, by_release=by_release, failures=failures
            )
            if detailed:
                return detailed
        return None

    async def _detailed(
        self,
        catalog: Catalog,
        wanted: dict[str, Any],
        found: Track,
        *,
        by_release: bool,
        failures: list[str] | None = None,
    ) -> Track | None:
        """``found`` of a search as its catalog lists it, still the wanted recording; None
        when its release is incomplete or it is another recording now."""
        # Search results can be partial: freeze the complete recording's identity.
        cid = CatalogId.parse(found.id)
        assert cid is not None
        song: CatalogTrack | None
        release: CatalogRelease | None
        try:
            if by_release:
                answer = await listed(catalog, cid.ref, found.release)
                song, release = answer if answer is not None else (None, None)
            else:
                song = await catalog.song(cid.ref.id)
                release = await catalog.album(song.album.id) if song.album else None
        except CatalogError as exc:
            if by_release and exc.kind in UNANSWERED:
                raise
            if failures is not None:
                failures.append(exc.kind)
            return None
        if song is None:
            return None
        detailed = Track(
            id=found.id,
            artist=song.artist,
            title=song.title,
            duration=song.seconds,
            album=release.title if release else "",
            isrc=song.isrc or "",
            explicit=song.explicit,
            clean=song.clean,
            release=str(song.album) if song.album else "",
            score=wanted["score"],
            reason=wanted["reason"],
            year=year_of(song.release_date or (release.release_date if release else None)),
            genres=genre_names(song.genres or (release.genres if release else ())),
        )
        if (
            not release
            or release.incomplete
            or not same_recording(wanted, detailed)
            or (
                wanted.get("strict_album")
                and wanted.get("album")
                and fold(detailed.album) != fold(wanted["album"])
            )
        ):
            return None
        return detailed
