"""Checks any catalog adapter can run against Shijhon's catalog interface:
what Shijhon relies on from an adapter's declaration and from the catalog it builds.

An adapter's own tests call them - with the catalog answering from recorded answers, or
live::

    from shijhon.catalog import contract

    def test_the_declaration() -> None:
        contract.check_declaration("example", adapter)

    async def test_the_catalog() -> None:
        await contract.check_catalog(catalog, contract.Sample(search="a term"))

Each raises :class:`ContractError` (an ``AssertionError``) that lists everything that does
not hold. Passing them does not make an adapter correct - its own tests show that it reads
its catalog's answers right - but an adapter that fails them will not work with Shijhon.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from shijhon.catalog import plugin
from shijhon.catalog.artwork import raster_type
from shijhon.catalog.base import Catalog, CatalogError, SearchResults
from shijhon.catalog.model import (
    CATALOG_KEY,
    ITEM_ID,
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    artist_data,
    artist_from_data,
    artwork_url,
    release_data,
    release_from_data,
    track_data,
    track_from_data,
)

ERROR_KINDS = ("not_found", "unauthorized", "rate_limited", "unavailable", "invalid")
_DATE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")
_ISRC = re.compile(r"[A-Z0-9]{12}")
_SECRET_NAME = re.compile(r"(^|_)(token|secret|password|passphrase|key|credential)s?($|_)")
_PLACEHOLDER = re.compile(r"\{[^}]*\}")


class ContractError(AssertionError):
    """What does not hold, one line each."""


@dataclass(frozen=True)
class Sample:
    """What to ask the catalog for. ``search``: a term that finds at least one album, and
    ideally songs and an artist too. ``album``: the album to open (the first one the search
    finds, when not given). ``artist``: the artist to open (else the first artist item the
    album, the search or ``artists_of`` names; a catalog that names none is not asked for
    one). ``missing_id``: an ID the catalog has nothing under. ``artwork``: also fetch one
    cover."""

    search: str
    album: str | None = None
    artist: str | None = None
    missing_id: str = "0"
    artwork: bool = True
    limit: int = 5


@dataclass
class _Found:
    problems: list[str] = field(default_factory=list)

    def check(self, holds: object, problem: str) -> bool:
        if not holds:
            self.problems.append(problem)
        return bool(holds)

    def done(self, what: str) -> None:
        if self.problems:
            lines = "\n".join(f"- {problem}" for problem in dict.fromkeys(self.problems))
            raise ContractError(f"{what} does not keep to the catalog contract:\n{lines}")


# --- the declaration ---------------------------------------------------------------------


def _keeps_secret(annotation: Any) -> bool:
    kind = plugin.plain(annotation)
    if kind in (SecretStr, Path):  # a file's path is no secret; what it holds stays there
        return True
    if typing.get_origin(kind) is dict:
        return typing.get_args(kind)[1:] == (SecretStr,)
    return False


def check_declaration(kind: str, adapter: plugin.Adapter) -> None:
    """The adapter's declaration (``plugin.Adapter``) as Shijhon will use it under ``kind``:
    its name, its settings beside Shijhon's own, their defaults and types, secrets, the
    choices and bound settings it names, and its ``problem``."""
    found = _Found()
    try:
        plugin.validate(kind, adapter)
    except plugin.AdapterError as exc:
        raise ContractError(str(exc)) from None
    found.check(isinstance(adapter.label, str) and adapter.label.strip(), "label: empty")
    found.check(callable(adapter.build), "build: not callable")
    model = adapter.settings
    fields = model.model_fields if model is not None else {}
    for key, info in fields.items():
        if _SECRET_NAME.search(key) and plugin.plain(info.annotation) not in (int, float, bool):
            found.check(
                _keeps_secret(info.annotation),
                f"{key}: named like a secret, so it must be a SecretStr (or a Path to a file,"
                " or dict[str, SecretStr])",
            )
    for key, words in adapter.words.items():
        found.check(words.label.strip(), f"words[{key}]: an empty label")
        found.check(
            words.group in ("connection", "albums", "advanced"),
            f"words[{key}]: group {words.group!r} is not a group of the Catalog page",
        )
    # The [catalog] section at its defaults, as problem() and build() are given it:
    # Shijhon's own settings with the adapter's, under its kind (registered for the moment
    # when it is not the installed one of that name).
    from shijhon.config import catalog_settings

    before = plugin._registered.get(kind)
    plugin.register(kind, adapter)
    try:
        defaults: Any = catalog_settings(kind)(kind=kind)
    except ValidationError:
        defaults = None
        found.check(False, "the settings' defaults do not validate")
    finally:
        plugin.unregister(kind)
        if before is not None:
            plugin.register(kind, before)
    if adapter.problem is not None and defaults is not None:
        try:
            missing = adapter.problem(defaults)
        except Exception as exc:
            found.check(False, f"problem() raised {type(exc).__name__} for the defaults")
        else:
            found.check(
                missing is None or isinstance(missing, plugin.Problem),
                "problem(): returns neither None nor a plugin.Problem",
            )
            if isinstance(missing, plugin.Problem):
                found.check(
                    missing.setting in fields,
                    f"problem(): names {missing.setting!r}, which is not one of the settings",
                )
                found.check(missing.row and missing.notice, "problem(): empty words")
    found.done(f"the adapter {kind!r}")


# --- the catalog ------------------------------------------------------------------------


def _ref(found: _Found, key: str, ref: Any, what: str) -> None:
    if not found.check(isinstance(ref, CatalogRef), f"{what}: not a CatalogRef"):
        return
    found.check(ref.catalog == key, f"{what}: of catalog {ref.catalog!r}, not {key!r}")
    found.check(
        ITEM_ID.fullmatch(ref.id),
        f"{what}: an ID Shijhon cannot carry (letters, digits and dots, at most 190)",
    )


def _refs(found: _Found, key: str, refs: Any, what: str) -> None:
    if found.check(isinstance(refs, tuple), f"{what}: not a tuple"):
        for ref in refs:
            _ref(found, key, ref, what)


def _artwork(found: _Found, template: Any, what: str) -> None:
    if template is None:
        return
    if not found.check(isinstance(template, str) and template, f"{what}: artwork is not text"):
        return
    found.check(
        template.startswith(("https://", "http://")), f"{what}: artwork is not an http(s) address"
    )
    other = [p for p in _PLACEHOLDER.findall(template) if p not in ("{w}", "{h}")]
    found.check(not other, f"{what}: artwork has placeholders besides {{w}} and {{h}}")


def _track(found: _Found, key: str, track: Any, what: str) -> None:
    if not found.check(isinstance(track, CatalogTrack), f"{what}: not a CatalogTrack"):
        return
    _ref(found, key, track.ref, what)
    found.check(isinstance(track.title, str) and track.title.strip(), f"{what}: no title")
    found.check(isinstance(track.artist, str), f"{what}: artist is not text")
    found.check(
        isinstance(track.duration_ms, int) and track.duration_ms > 0, f"{what}: no duration"
    )
    found.check(isinstance(track.disc, int) and track.disc >= 1, f"{what}: disc below 1")
    found.check(isinstance(track.number, int) and track.number >= 0, f"{what}: number below 0")
    found.check(
        track.isrc is None or _ISRC.fullmatch(track.isrc),
        f"{what}: ISRC not 12 upper-case letters and digits (or None)",
    )
    found.check(not (track.explicit and track.clean), f"{what}: both explicit and clean")
    if track.album is not None:
        _ref(found, key, track.album, f"{what} (its album)")
    _refs(found, key, track.artist_refs, f"{what} (its artists)")
    found.check(isinstance(track.genres, tuple), f"{what}: genres not a tuple")
    found.check(
        track.release_date is None or _DATE.fullmatch(track.release_date),
        f"{what}: release date not YYYY, YYYY-MM or YYYY-MM-DD",
    )
    _artwork(found, track.artwork_template, what)
    found.check(track_from_data(track_data(track)) == track, f"{what}: changes when saved")


def _release(found: _Found, key: str, release: Any, what: str) -> None:
    if not found.check(isinstance(release, CatalogRelease), f"{what}: not a CatalogRelease"):
        return
    _ref(found, key, release.ref, what)
    found.check(isinstance(release.title, str) and release.title.strip(), f"{what}: no title")
    found.check(isinstance(release.artist, str), f"{what}: artist is not text")
    found.check(isinstance(release.kind, ReleaseKind), f"{what}: kind is not a ReleaseKind")
    found.check(
        release.release_date is None or _DATE.fullmatch(release.release_date),
        f"{what}: release date not YYYY, YYYY-MM or YYYY-MM-DD",
    )
    found.check(not (release.explicit and release.clean), f"{what}: both explicit and clean")
    found.check(
        release.track_count is None or release.track_count >= 0, f"{what}: a negative track count"
    )
    _refs(found, key, release.artist_refs, f"{what} (its artists)")
    _artwork(found, release.artwork_template, what)
    if found.check(isinstance(release.tracks, tuple), f"{what}: tracks not a tuple"):
        for track in release.tracks:
            _track(found, key, track, f"{what}, track {getattr(track, 'title', '?')!r}")
    found.check(release_from_data(release_data(release)) == release, f"{what}: changes when saved")


def _artist(found: _Found, key: str, artist: Any, what: str) -> None:
    if not found.check(isinstance(artist, CatalogArtist), f"{what}: not a CatalogArtist"):
        return
    _ref(found, key, artist.ref, what)
    found.check(isinstance(artist.name, str) and artist.name.strip(), f"{what}: no name")
    _artwork(found, artist.artwork_template, what)
    found.check(artist_from_data(artist_data(artist)) == artist, f"{what}: changes when saved")


async def _missing(
    found: _Found, call: Callable[[], Awaitable[Any]], what: str, *, empty: bool = False
) -> None:
    """Nothing under an ID: "not found" (``empty``: or an empty answer), never another error
    and never a URL in the reason."""
    try:
        answer = await call()
    except CatalogError as exc:
        found.check(exc.kind == "not_found", f"{what}: {exc.kind} for an unknown ID, not not_found")
        found.check("://" not in str(exc), f"{what}: an address in the error's reason")
    except Exception as exc:
        found.check(False, f"{what}: {type(exc).__name__} for an unknown ID, not CatalogError")
    else:
        found.check(empty and answer == (), f"{what}: an answer for an unknown ID")


async def check_catalog(catalog: Catalog, sample: Sample) -> None:
    """The catalog behind the interface (``base.Catalog``), asked for ``sample``: a
    search, the first album it finds with its tracks, one of its songs (by ID and by ISRC),
    its artist with releases and top songs, the artist items of what the search showed,
    one cover, and IDs it does not have. Every answer is checked for what Shijhon builds
    on: the catalog's key in every reference, IDs it can carry in its own, typed and
    complete items that survive being saved, limits kept, and errors by their kind."""
    found = _Found()
    key = getattr(catalog, "key", None)
    if not found.check(
        isinstance(key, str) and CATALOG_KEY.fullmatch(key),
        "key: lower-case letters and digits, starting with a letter",
    ):
        found.done("the catalog")
    assert isinstance(key, str)
    found.check(isinstance(getattr(catalog, "region", None), str), "region: not text")
    found.check(ITEM_ID.fullmatch(sample.missing_id), "the sample's missing ID is not an ID")
    limit = max(1, sample.limit)

    results = await catalog.search(sample.search, limit)
    if not found.check(isinstance(results, SearchResults), "search: not SearchResults"):
        found.done("the catalog")
    for name in ("artists", "albums", "songs"):
        items = getattr(results, name)
        found.check(isinstance(items, tuple), f"search: {name} not a tuple")
        found.check(len(items) <= limit, f"search: more {name} than the limit")
    for artist in results.artists:
        _artist(found, key, artist, f"search artist {getattr(artist, 'name', '?')!r}")
    for album in results.albums:
        _release(found, key, album, f"search album {getattr(album, 'title', '?')!r}")
    for song in results.songs:
        _track(found, key, song, f"search song {getattr(song, 'title', '?')!r}")
    if not found.check(results.albums, "search: the sample's term finds no album"):
        found.done("the catalog")

    wanted = sample.album or results.albums[0].ref.id
    release = await catalog.album(wanted)
    _release(found, key, release, f"album {wanted}")
    found.check(
        getattr(getattr(release, "ref", None), "id", None) == wanted,
        "album: another item than the one asked for",
    )
    if found.check(release.tracks, "album: no tracks"):
        found.check(
            all(t.album == release.ref for t in release.tracks),
            "album: a track that does not name the album",
        )
        first = release.tracks[0]
        song = await catalog.song(first.ref.id)
        _track(found, key, song, f"song {first.ref.id}")
        found.check(song.ref == first.ref, "song: another item than the one asked for")
        found.check(song.album is not None, "song: without its album")
        with_isrc = next((t for t in release.tracks if t.isrc), None)
        if with_isrc is not None and with_isrc.isrc is not None:
            same = await catalog.songs_by_isrc(with_isrc.isrc)
            if found.check(isinstance(same, tuple), "songs_by_isrc: not a tuple"):
                for track in same:
                    _track(found, key, track, f"songs_by_isrc {with_isrc.isrc}")
                found.check(
                    all(t.isrc == with_isrc.isrc for t in same),
                    "songs_by_isrc: a song with another ISRC",
                )

    credits = await catalog.artists_of(
        tuple(s.ref.id for s in results.songs), tuple(a.ref.id for a in results.albums)
    )
    known = {f"songs:{s.ref.id}" for s in results.songs}
    known |= {f"albums:{a.ref.id}" for a in results.albums}
    if found.check(isinstance(credits, dict), "artists_of: not a dict"):
        found.check(set(credits) <= known, "artists_of: keys other than songs:<id>, albums:<id>")
        for item, refs in credits.items():
            _refs(found, key, refs, f"artists_of {item}")
            found.check(refs, f"artists_of {item}: listed without artists (leave it out)")

    artists = [*release.artist_refs, *(r for refs in credits.values() for r in refs)]
    artists += [a.ref for a in results.artists]
    artists += [r for t in release.tracks for r in t.artist_refs]
    artists += [r for s in results.songs for r in s.artist_refs]
    # (A catalog may name no artist items at all - display credits only: none to open.)
    artist_id = sample.artist or (artists[0].id if artists else None)
    if artist_id is not None:
        _artist(found, key, await catalog.artist(artist_id), f"artist {artist_id}")
        releases = await catalog.artist_releases(artist_id)
        if found.check(isinstance(releases, tuple), "artist_releases: not a tuple"):
            for listed_release in releases:
                title = getattr(listed_release, "title", "?")
                _release(found, key, listed_release, f"artist release {title!r}")
        top = await catalog.top_songs(artist_id, 2)
        if found.check(isinstance(top, tuple), "top_songs: not a tuple"):
            found.check(len(top) <= 2, "top_songs: more than the limit")
            for track in top:
                _track(found, key, track, f"top song {getattr(track, 'title', '?')!r}")

    if sample.artwork:
        url = artwork_url(release.artwork_template, 300)
        if found.check(url, "album: no artwork to fetch (or check with artwork=False)"):
            assert url is not None
            data, content_type = await catalog.artwork(url)
            found.check(isinstance(data, bytes) and data, "artwork: no image bytes")
            found.check(
                isinstance(content_type, str) and content_type.startswith("image/"),
                "artwork: the content type is not an image's",
            )
            # Shijhon serves and keeps raster images only, by their bytes (an SVG could
            # carry script into Shijhon's origin): anything else is never shown.
            found.check(
                isinstance(data, bytes) and raster_type(data) is not None,
                "artwork: not a JPEG, PNG, GIF or WebP image by its bytes (Shijhon serves no"
                " other, whatever its content type)",
            )
    try:
        await catalog.artwork("https://catalog-contract.invalid/300x300.jpg")
    except CatalogError as exc:
        found.check(
            exc.kind == "invalid",
            f"artwork: {exc.kind} for a foreign address, not invalid (was it asked for?)",
        )
        found.check("://" not in str(exc), "artwork: an address in the error's reason")
    except Exception as exc:
        found.check(False, f"artwork: {type(exc).__name__} for a foreign address")
    else:
        found.check(False, "artwork: fetched an address that is not the catalog's")

    gone = sample.missing_id
    await _missing(found, lambda: catalog.album(gone), "album")
    await _missing(found, lambda: catalog.song(gone), "song")
    await _missing(found, lambda: catalog.artist(gone), "artist")
    await _missing(found, lambda: catalog.artist_releases(gone), "artist_releases", empty=True)
    await _missing(found, lambda: catalog.top_songs(gone, 2), "top_songs", empty=True)
    nothing = await catalog.songs_by_isrc("ZZZZZ0000000")
    found.check(nothing == (), "songs_by_isrc: an answer for an ISRC nobody has")
    nobody = await catalog.artists_of((), ())
    found.check(nobody == {}, "artists_of: an answer for nothing asked")
    check = getattr(catalog, "check", None)
    if callable(check):  # optional: one small request for the dashboard's "Check now"
        await check()
    found.done(f"the catalog {key!r}")
