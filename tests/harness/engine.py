"""The placeholder engine wired to a test Navidrome (used inside async tests)."""

from __future__ import annotations

import time
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from shijhon.catalog.base import CatalogError
from shijhon.catalog.model import (
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
)
from shijhon.cleanup import Cleanup
from shijhon.fill.fills import Fills
from shijhon.fill.refresh import Refresh
from shijhon.fill.track_lists import DAY, TrackLists
from shijhon.matching.matcher import Matcher
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.navidrome.usage import UsageSource
from shijhon.placeholders.engine import PlaceholderEngine
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.silence import SilenceMaker
from shijhon.store import Store
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance

PLACEHOLDER_FOLDER = "_shijhon"


@dataclass
class EngineParts:
    engine: PlaceholderEngine
    navidrome: NavidromeService
    scans: ScanCoordinator
    silence: SilenceMaker
    store: Store
    layout: Layout


@asynccontextmanager
async def engine_for(nd: NavidromeInstance, state: Path) -> AsyncIterator[EngineParts]:
    service = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD, client_name="shijhon")
    store = await Store.open(state / "shijhon.sqlite3")
    scans = ScanCoordinator(service)
    silence = SilenceMaker()
    layout = Layout(nd.music, PLACEHOLDER_FOLDER)
    engine = PlaceholderEngine(
        layout=layout, navidrome=service, scans=scans, silence=silence, store=store
    )
    try:
        yield EngineParts(engine, service, scans, silence, store, layout)
    finally:
        await store.close()
        await service.aclose()


def catalog_release(
    key: str,
    title: str,
    artist: str,
    count: int,
    *,
    kind: ReleaseKind = ReleaseKind.ALBUM,
    date: str | None = "2020-05-06",
    seconds: int = 3,
    discs: int = 1,
    clean: bool = False,
    track_artist: str | None = None,
) -> CatalogRelease:
    ref = CatalogRef("test", key)
    per_disc = -(-count // discs)
    tracks = tuple(
        CatalogTrack(
            ref=CatalogRef("test", f"{key}-{n}"),
            title=f"{title} Song {n}",
            artist=track_artist or artist,
            duration_ms=seconds * 1000,
            disc=(n - 1) // per_disc + 1,
            number=(n - 1) % per_disc + 1,
            isrc=f"QZ{zlib.crc32(key.encode()) % 1000:03d}26{n:05d}",
            album=ref,
        )
        for n in range(1, count + 1)
    )
    return CatalogRelease(
        ref=ref,
        title=title,
        artist=artist,
        kind=kind,
        release_date=date,
        tracks=tracks,
        clean=clean,
    )


def applied_later(release: CatalogRelease) -> CatalogRelease:
    """What the library pass's reading makes of ``listed_later``: the second track's
    length and the track more - the first keeps its title (only lengths are corrected)."""
    later = listed_later(release)
    return replace(later, tracks=(release.tracks[0], *later.tracks[1:]))


def listed_later(release: CatalogRelease) -> CatalogRelease:
    """The release as its catalog lists it later: its first track retitled, its second
    longer, and one track more at the end."""
    first, second, *rest = release.tracks
    last = release.tracks[-1]
    more = replace(
        last,
        ref=CatalogRef("test", f"{release.ref.id}-more"),
        title=f"{release.title} Song More",
        number=last.number + 1,
        isrc=f"{(last.isrc or 'QZ00026')[:7]}99999",
    )
    tracks = (
        replace(first, title=f"{first.title} (Corrected)"),
        replace(second, duration_ms=second.duration_ms + 2000),
        *rest,
        more,
    )
    return replace(release, tracks=tracks)


class _Listing:
    """A catalog that lists one release, as given."""

    key = "test"

    def __init__(self, release: CatalogRelease) -> None:
        self.release = release
        self.asked = 0

    async def album(self, album_id: str) -> CatalogRelease:
        self.asked += 1
        return self.release


class _Idle:
    """What the track lists ask of the fills: nothing waits, the catalog does not rest."""

    resting_until = 0.0
    rest_seconds = 60.0
    clock = staticmethod(time.time)

    async def wait_idle(self) -> None:
        return None


async def track_lists_for(
    engine: PlaceholderEngine,
    store: Store,
    navidrome: NavidromeService,
    listed: CatalogRelease,
    *,
    days: float = 31.0,
) -> TrackLists:
    """The library pass's reading of added releases' track lists over an engine, with the
    catalog listing ``listed`` and the clock ``days`` on: that release is due, and every
    other one in the database (other tests') was read again just then."""
    lists = TrackLists(store, Refresh(store, navidrome, engine, _Listing(listed)), _Idle())  # type: ignore[arg-type]
    at = time.time() + days * DAY
    lists.clock = lambda: at
    await store.execute(
        "INSERT INTO release_refreshes (ref, checked_at, outcome)"
        " SELECT ref, ?, 'same' FROM releases WHERE ref != ?"
        " ON CONFLICT (ref) DO UPDATE SET checked_at = excluded.checked_at, outcome = 'same'",
        [at, str(listed.ref)],
    )
    return lists


class Editions:
    """A catalog that has these releases and nothing else (``catalog_release``'s: the
    key "test"), for fills of an owned album from one edition or another."""

    key = "test"
    region = ""

    def __init__(self, *releases: CatalogRelease) -> None:
        self.releases = {release.ref.id: release for release in releases}

    async def album(self, ident: str) -> CatalogRelease:
        if ident not in self.releases:
            raise CatalogError("not_found", "no such release")
        return self.releases[ident]


EDITIONS_ARTIST = "R Editioner"


def edition(key: str, count: int, title: str = "R Editions") -> CatalogRelease:
    """An edition of the album ``title`` with ``count`` tracks: every edition of it has
    the same first tracks at the same numbers ("<title> Song 1", ...), and a track ID of
    its own for each."""
    return catalog_release(key, title, EDITIONS_ARTIST, count)


def fills_for(
    engine: PlaceholderEngine, store: Store, service: NavidromeService, *releases: CatalogRelease
) -> Fills:
    """The fills of an engine, over a catalog with these releases."""
    catalog = Editions(*releases)
    return Fills(store, service, engine, Matcher(catalog), scope="test.xx")  # type: ignore[arg-type]


def edition_with(key: str, title: str, songs: dict[int, int]) -> CatalogRelease:
    """An edition of the album ``title`` that has these of its songs ("<title> Song n"),
    each at the track number given: editions that lack a song the other has, or number
    one differently."""
    base = catalog_release(key, title, EDITIONS_ARTIST, max(songs))
    tracks = tuple(replace(base.tracks[n - 1], number=number) for n, number in songs.items())
    return replace(base, tracks=tracks)


def cleanup_for(
    engine: PlaceholderEngine,
    nd: NavidromeInstance,
    state: Path,
    fills: Fills,
    usage: UsageSource | None = None,
) -> Cleanup:
    """The cleanup of an engine, reading Navidrome's own database for who uses what (or
    ``usage``): its ``take_out_now`` is the removal a release change goes through."""
    source = usage or UsageSource(nd.data / "navidrome.db", export=False)
    return Cleanup(engine, database=state / "shijhon.sqlite3", usage=source, filler=fills)
