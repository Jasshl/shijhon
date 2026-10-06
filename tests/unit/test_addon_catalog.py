"""The catalog of an add-on (``shijhon.catalog.addon``): how the add-on protocol's
catalog answers are read - in the shapes add-ons answer with, and with fields missing -,
what a view costs the add-on, IDs, artists by name, a song's album, artwork, and failures.
The add-on is the harness's invented catalog behind a mock transport."""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import anyio
import httpx
import pytest

from shijhon.catalog import contract, plugin
from shijhon.catalog.addon import (
    KIND,
    MAX_ARTWORK_BYTES,
    NO_CATALOG,
    NO_IDENTITY,
    NOT_ENABLED,
    AddonCatalog,
    adapter,
    artwork_address,
    unmarked,
)
from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.model import CATALOG_KEY, CatalogRef, ReleaseKind
from shijhon.catalog.paced import PacedCatalog
from shijhon.catalog.setup import build_catalog
from shijhon.config import load_settings
from shijhon.delivery import pacing
from shijhon.delivery.addon import (
    Addon,
    catalog_key,
    item_id,
    of_catalog,
    own_id,
    own_track,
    service_tag,
    tagged,
    untagged,
)
from shijhon.delivery.pacing import AddonPace, Limits, Paces
from tests.harness.addon_catalog import IMAGES, FakeCatalog
from tests.harness.library import cover_image

NAME = "Fake"
BASE = "https://addon.example.invalid/cfg"
SERVICE = "test.fake"  # the add-on's manifest ID
T = service_tag(SERVICE)  # ... and its tag, at the start of every item ID
I = "i" + T  # an artist item  # noqa: E741


class Server:
    """An add-on with a catalog behind a mock transport: counts requests."""

    def __init__(self, held: FakeCatalog, resources: tuple[str, ...] | None = None) -> None:
        self.held = held
        self.resources = resources or ("stream", "isrc", "search", "catalog")
        self.service = SERVICE  # its manifest's ID
        self.asked: list[str] = []
        self.status: dict[str, httpx.Response] = {}  # endpoint -> the answer instead
        self.wait: anyio.Event | None = None  # answers wait for it
        self.delay: dict[str, float] = {}  # endpoint -> seconds before its answers

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.raw_path.decode()).split("?")[0].removeprefix("/cfg/")
        endpoint = path.split("/", 1)[0]
        self.asked.append(endpoint if endpoint in ("manifest.json", "search") else path)
        if self.wait is not None:
            await self.wait.wait()
        if endpoint in self.delay:
            await anyio.sleep(self.delay[endpoint])
        if endpoint in self.status:
            return self.status[endpoint]
        if endpoint == "manifest.json":
            manifest = {"id": self.service, "name": NAME, "resources": list(self.resources)}
            return httpx.Response(200, json=manifest)
        answers = self.held.answers
        if path in answers or endpoint in answers:
            answer = answers.get(path, answers.get(endpoint))
            if isinstance(answer, bytes):  # a body as it is
                return httpx.Response(200, content=answer)
            return httpx.Response(200, json=answer)
        if endpoint == "search":
            return httpx.Response(200, json=self.held.search(request.url.params.get("q", "")))
        ident = path.split("/", 1)[1] if "/" in path else ""
        answer = self.held.album(ident) if endpoint == "album" else self.held.artist(ident)
        return httpx.Response(404) if answer is None else httpx.Response(200, json=answer)

    def count(self, what: str) -> int:
        return sum(1 for asked in self.asked if asked.split("/", 1)[0] == what)


@dataclass
class Listed:
    """An enabled add-on, as the registry lists it."""

    id: int
    name: str
    addon: Addon
    pace: AddonPace | None = None


@dataclass
class Registry:
    """The installation's add-ons, as far as the catalog asks."""

    sources: list[Listed]
    paces: Paces = field(default_factory=Paces)
    cooled: set[int] = field(default_factory=set)

    async def enabled(self) -> list[Listed]:
        return self.sources

    def cooling(self, source_id: int) -> bool:
        return source_id in self.cooled


@dataclass
class Images:
    """The image host: counts requests, answers a JPEG (or what the test sets)."""

    asked: list[str] = field(default_factory=list)
    answer: httpx.Response | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(str(request.url))
        if self.answer is not None:
            return self.answer
        return httpx.Response(
            200, content=cover_image("blue").read_bytes(), headers={"content-type": "image/jpeg"}
        )


@dataclass
class World:
    catalog: AddonCatalog
    server: Server
    images: Images
    registry: Registry
    pace: AddonPace | None
    hosts: dict[str, list[str]]  # host -> its addresses (any other: a public one)


def world(
    shape: str = "plain",
    *,
    prefix: str = "",
    resources: tuple[str, ...] | None = None,
    limits: Limits | None = None,
    timeout: float = 5.0,
) -> World:
    server = Server(FakeCatalog(shape, prefix=prefix), resources)
    paces = Paces()
    paces.apply([(BASE, limits)] if limits is not None else [])
    pace = paces.of(BASE) if limits is not None else None
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    registry = Registry([Listed(1, NAME, Addon(BASE, None, http, pace), pace)], paces)
    images = Images()
    covers = httpx.AsyncClient(transport=httpx.MockTransport(images.handle))
    hosts: dict[str, list[str]] = {}

    async def resolve(host: str, port: int) -> list[str]:
        return hosts.get(host, ["1.2.3.4"])

    catalog = AddonCatalog(
        NAME,
        registry,  # type: ignore[arg-type]
        covers,
        timeout=timeout,
        resolver=resolve,
    )
    return World(catalog, server, images, registry, pace, hosts)


def ref(w: World, own: str, kind: str = "") -> str:
    """The catalog ID of the add-on's item ``own`` (``kind``: "i" for an artist)."""
    return kind + (tagged(SERVICE, w.server.held.id(own)) or "")


# --- IDs --------------------------------------------------------------------------------------


def test_an_item_s_id_is_the_add_on_s_own_and_reads_back() -> None:
    owns = ("12345", "lib:t101", "a-b_c.d/e f", "Ünïcode", "x" * 190, " a", "a ", "...", "a/../b")
    for own in owns:
        ident = item_id(own)
        assert ident is not None and all(ch.isalnum() or ch == "." for ch in ident)
        assert own_id(ident) == own  # exactly: nothing trimmed
    assert len({item_id(own) for own in owns}) == len(owns)
    assert item_id(12345) == "12345" and item_id("lib:t101") == "lib.3At101"
    assert item_id(10**400) == "1" + "0" * 400 or item_id(10**400) is None  # never an error
    assert item_id(None) is None and item_id("") is None and item_id(True) is None
    assert item_id("-" * 64) is None  # too long once translated: no item
    assert item_id("x" * 1_000_000) is None and item_id("\ud800") is None  # (a lone surrogate)
    # No path segment: asked for, they would be another address of the add-on.
    assert item_id(".") is None and item_id("..") is None
    # Not IDs made here - also another spelling of one ("aA" has one ID).
    for other in ("a.3", "a.3a", "a.ZZ", ".", "a-b", "n.x", ".FF", "a.41", ".2E", ".2E.2E"):
        assert own_id(other) is None, other


def test_the_key_tells_add_ons_apart_and_is_the_name_s_alone() -> None:
    assert catalog_key("Fake") == catalog_key("Fake")
    names = ("Fake", "fake", "Fa-ke", "Fa ke", "Other", "音楽", "abcdefghijklmnop88056",
             "abcdefghijklmnop94100")  # fmt: skip
    keys = {catalog_key(name) for name in names}
    assert len(keys) == len(names)  # (a long digest: the last two shared a short one)
    for key in keys:
        assert key != plugin.NONE and CATALOG_KEY.fullmatch(key)
        assert key.startswith("addon") and key != "demo"


def test_a_song_of_an_add_on_s_catalog_names_that_add_on_s_own_track() -> None:
    """Its catalog reference says whose it is - the add-on's name - and of which service
    (the add-on's manifest ID): only that add-on, while it is that service, has its ID."""
    item = tagged("svc.one", "lib:t101")
    assert item == service_tag("svc.one") + "lib.3At101" and untagged("svc.one", item) == "lib:t101"
    song = f"{catalog_key('Fake')}:{item}"
    assert own_track("Fake", "svc.one", song) == "lib:t101" and of_catalog("Fake", song)
    assert own_track("Other", "svc.one", song) is None  # another add-on's catalog
    assert not of_catalog("Other", song)
    # The add-on was pointed at another service under its name: not that one's ID.
    assert own_track("Fake", "svc.two", song) is None and of_catalog("Fake", song)
    assert own_track("Fake", "svc.one", "demo:900000001") is None
    assert own_track("Fake", "svc.one", None) is None
    assert own_track("Fake", "svc.one", f"{catalog_key('Fake')}:") is None
    other_spelling = f"{catalog_key('Fake')}:{service_tag('svc.one')}lib.3At.3101"
    assert own_track("Fake", "svc.one", other_spelling) is None
    assert tagged("svc.one", "x" * 185) is None and tagged("svc.one", None) is None


# --- the answers, in the shapes add-ons send them -------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("shape", ["plain", "renamed", "rich"])
@pytest.mark.parametrize("prefix", ["", "lib:"])
async def test_the_contract_holds_for_every_shape(shape: str, prefix: str) -> None:
    w = world(shape, prefix=prefix)
    contract.check_declaration(KIND, adapter)
    await contract.check_catalog(w.catalog, contract.Sample(search="mara venn"))
    assert w.images.asked  # the album's cover was fetched


@pytest.mark.anyio
@pytest.mark.parametrize("shape", ["plain", "renamed", "rich"])
async def test_a_search_an_album_and_an_artist_are_read(shape: str) -> None:
    w = world(shape, prefix="lib:")
    key = w.catalog.key
    found = await w.catalog.search("venn", 20)
    assert [a.name for a in found.artists] == ["Mara Venn"]
    assert found.artists[0].ref.id == ref(w, "ar1", "i")
    assert {a.title for a in found.albums} == {"Glass Rivers", "Low Tide - Single"}
    assert all(a.ref.catalog == key and not a.tracks for a in found.albums)
    single = next(a for a in found.albums if a.title.startswith("Low Tide"))
    album = next(a for a in found.albums if a.title == "Glass Rivers")
    assert single.kind is ReleaseKind.SINGLE and album.kind is ReleaseKind.ALBUM
    assert album.year == 2019 and album.artist == "Mara Venn"
    assert album.track_count == (None if shape == "renamed" else 4)
    songs = {s.title: s for s in found.songs}
    assert songs["Salt Meadow"].isrc == "ZZSHA0000102" and songs["Slow Thaw"].isrc is None
    assert songs["Salt Meadow"].duration_ms == 5000  # whole seconds
    # A song of a search names its album by title only: no album reference, no number.
    assert all(s.album is None and s.number == 0 and s.disc == 1 for s in found.songs)
    assert songs["Two Kettles"].album_title == "North Window"
    assert not any(s.explicit or s.clean for s in found.songs)

    release = await w.catalog.album(album.ref.id)
    assert release.ref == album.ref and release.title == "Glass Rivers"
    assert [(t.disc, t.number, t.title) for t in release.tracks] == [
        (1, 1, "Glass Rivers"),
        (1, 2, "Salt Meadow"),
        (1, 3, "Harbor Lights"),
        (1, 4, "Slow Thaw"),
    ]
    assert all(t.album == release.ref and t.album_title == "Glass Rivers" for t in release.tracks)
    assert release.track_count == 4 and not release.incomplete
    assert release.release_date == ("2019-03-08" if shape == "renamed" else "2019")
    assert all(t.release_date == release.release_date for t in release.tracks)

    artist = await w.catalog.artist(found.artists[0].ref.id)
    assert artist.name == "Mara Venn" and artist.ref == found.artists[0].ref
    releases = await w.catalog.artist_releases(artist.ref.id)
    # An artist's own albums often name no artist: they are that artist's.
    assert [(r.title, r.artist) for r in releases] == [
        ("Glass Rivers", "Mara Venn"),
        ("Low Tide - Single", "Mara Venn"),
    ]
    top = await w.catalog.top_songs(artist.ref.id, 3)
    assert [t.title for t in top] == ["Salt Meadow", "Glass Rivers", "Low Tide"]
    assert await w.catalog.top_songs(artist.ref.id, 0) == ()


@pytest.mark.anyio
async def test_a_search_is_one_request_cut_to_the_count_asked_for() -> None:
    w = world()
    assert len((await w.catalog.search("a", 2)).songs) == 2
    assert len((await w.catalog.search("a", 5)).songs) == 5  # the same answer, cut again
    assert w.server.count("search") == 1 and w.server.count("manifest.json") == 1
    assert (await w.catalog.search("nothing of that name", 5)).songs == ()


@pytest.mark.anyio
async def test_an_artist_page_is_one_request() -> None:
    w = world()
    ident = ref(w, "ar1", "i")
    async with anyio.create_task_group() as tg:  # the page's three questions, at once
        tg.start_soon(w.catalog.artist, ident)
        tg.start_soon(w.catalog.artist_releases, ident)
        tg.start_soon(w.catalog.top_songs, ident)
    await w.catalog.top_songs(ident, 2)
    assert w.server.asked == ["manifest.json", "artist/ar1"]


@pytest.mark.anyio
async def test_what_an_answer_lacks_is_left_out_not_guessed() -> None:
    w = world()
    held = w.server.held
    good = {"id": "t9", "title": "Kept", "artist": "Mara Venn", "duration": 3}
    held.answers["album/al9"] = {
        "name": "Loose Ends",  # (a title under its other name; no ID: the album asked for)
        "artist": {"name": "Mara Venn"},
        "year": "not a year",
        "trackCount": "many",
        "artworkURL": "ftp://images.fake.test/x.jpg",
        "tracks": [
            good,
            {"id": "t10", "title": "No length", "artist": "Mara Venn"},
            {"id": "t11", "title": "Zero", "duration": 0},
            {"title": "No ID", "duration": 3},
            {"id": "t12", "duration": 3},
            "not a track",
            good,  # twice: once
            {"id": 13, "title": 1984, "durationMs": 2500, "isrc": "zz-sha-00-00913"},
            {"id": "t14", "title": "Junk ISRC", "duration": "4", "isrc": "-", "artist": None},
        ],
    }
    release = await w.catalog.album(T + "al9")
    assert release.ref.id == T + "al9" and release.title == "Loose Ends"
    assert release.artist == "Mara Venn" and release.release_date is None
    assert release.artwork_template is None  # not an http(s) address
    assert [(t.number, t.title) for t in release.tracks] == [
        (1, "Kept"),
        (8, "1984"),
        (9, "Junk ISRC"),
    ]
    assert release.incomplete and release.track_count == 9  # never filled into an owned album
    numbered, junk = release.tracks[1], release.tracks[2]
    assert numbered.ref.id == T + "13" and numbered.duration_ms == 2500
    assert numbered.isrc == "ZZSHA0000913" and junk.isrc is None
    assert junk.artist == "Mara Venn" and junk.duration_ms == 4000  # the album's credit
    # A search answer with odd parts: the parts that can be read are.
    held.answers["search"] = {"tracks": None, "albums": [{"id": "al9"}, 7], "artists": "none"}
    assert await w.catalog.search("anything", 5) == SearchResults()
    held.answers["search"] = ["not", "an", "object"]
    with pytest.raises(CatalogError) as failed:
        await w.catalog.search("other", 5)
    assert failed.value.kind == "invalid"
    held.answers["album/al8"] = {"error": "no such album"}  # answered 200, without the album
    # (The last ones: "al9" spelled otherwise, and without its service's tag.)
    for missing in (T + "al8", T + "al7", "not.an.id", T + "al.39", "al9"):
        with pytest.raises(CatalogError) as failed:
            await w.catalog.album(missing)
        assert failed.value.kind == "not_found"


@pytest.mark.anyio
async def test_an_add_on_pointed_at_another_service_does_not_get_the_first_one_s_items() -> None:
    """The key is the add-on's name; an item also says which service it is of (the
    manifest's ID). Under the same name, another service is asked for none of the IDs the
    first one gave, and what was remembered of the first is dropped."""
    w = world()
    found = await w.catalog.search("venn", 5)
    album, artist, song = found.albums[0], found.artists[0], found.songs[0]
    assert album.ref.id.startswith(T) and artist.ref.id.startswith("i" + T)
    await w.catalog.album(album.ref.id)
    w.server.service = "test.other"  # the add-on's address was changed: another service
    w.registry.sources[0].addon = Addon(BASE, None, w.registry.sources[0].addon.http, w.pace)
    w.server.asked.clear()
    for ask in (
        lambda: w.catalog.album(album.ref.id),
        lambda: w.catalog.artist(artist.ref.id),
        lambda: w.catalog.top_songs(artist.ref.id),
    ):
        with pytest.raises(CatalogError) as failed:
            await ask()
        assert failed.value.kind == "not_found" and "the service" in failed.value.reason
    assert w.server.asked == ["manifest.json"]  # nothing of the first service was asked for
    # The new service's own items are its own, under another tag.
    w.catalog._found.cache_clear()
    again = await w.catalog.search("venn", 5)
    other = service_tag("test.other")
    assert again.albums[0].ref.id == other + album.ref.id[len(T) :]
    assert (await w.catalog.album(again.albums[0].ref.id)).tracks
    with pytest.raises(CatalogError):
        await w.catalog.song(song.ref.id)  # (what the first service showed is forgotten)
    # ... and a song of the first service is not asked for there by its old ID.
    reference = str(song.ref)
    assert own_track(NAME, "test.fake", reference) and not own_track(NAME, "test.other", reference)


@pytest.mark.anyio
async def test_an_answer_that_comes_late_from_the_service_before_names_no_artist() -> None:
    """A search asked of the first service answers after the add-on is the second one: the
    artist it names is not opened at the second service by the first one's ID."""
    w = world()
    await w.catalog.check()  # (the first service's manifest is read)
    w.server.wait = anyio.Event()
    late: list[Any] = []

    async def slow() -> None:
        late.append(await w.catalog.search("venn", 5))

    async with anyio.create_task_group() as tg:
        tg.start_soon(slow)
        await anyio.sleep(0.05)  # the first service is being asked
        first, w.server.wait = w.server.wait, None
        w.server.service = "test.other"
        w.registry.sources[0].addon = Addon(BASE, None, w.registry.sources[0].addon.http, w.pace)
        await w.catalog.search("brandt", 5)  # the add-on is the second service now
        first.set()
    assert late and late[0].artists[0].ref.id == "i" + T + "ar1"  # the first service's item
    w.server.held.acts[0].id = "moved"  # the second service has her under another ID
    w.server.asked.clear()
    page = await w.catalog.artist("nMara.20Venn")  # found by a search of the second
    assert page.ref.id == "i" + service_tag("test.other") + "moved"
    assert w.server.asked == ["search", "artist/moved"]


@pytest.mark.anyio
async def test_an_add_on_without_an_id_cannot_be_the_catalog() -> None:
    """Its items could not be told from another service's under the same name."""
    w = world()
    w.server.service = ""
    for ask in (lambda: w.catalog.search("venn", 5), lambda: w.catalog.check()):
        with pytest.raises(CatalogError) as failed:
            await ask()
        assert failed.value.kind == "invalid" and failed.value.reason == NO_IDENTITY
    assert w.server.count("search") == 0
    song = f"{catalog_key(NAME)}:{tagged('', 'lib:t1')}"
    assert own_track(NAME, "", song) is None  # ... and is never asked by an ID of its own


@pytest.mark.anyio
async def test_an_answer_about_another_item_is_not_read_as_the_one_asked_for() -> None:
    w = world()
    held = w.server.held
    held.answers["album/al1"] = held.album("al3")  # it says it is al3
    held.answers["artist/ar1"] = held.artist("ar2")
    for ask in (lambda: w.catalog.album(T + "al1"), lambda: w.catalog.artist(I + "ar1")):
        with pytest.raises(CatalogError) as failed:
            await ask()
        assert failed.value.kind == "invalid" and "another" in failed.value.reason
    with pytest.raises(CatalogError):
        await w.catalog.song(T + "t301")  # nothing of it was kept
    held.answers["album/al1"] = {**(held.album("al1") or {}), "id": 0}  # an ID of another type
    with pytest.raises(CatalogError):
        await w.catalog.album(T + "al1")


@pytest.mark.anyio
async def test_an_album_that_lists_fewer_tracks_than_it_has_is_incomplete() -> None:
    """Never filled into an owned album: a track left out, a list shorter than the count
    the album names, or no list at all."""
    w = world()
    held = w.server.held
    whole = held.album("al1") or {}
    assert not (await w.catalog.album(T + "al1")).incomplete
    held.answers["album/al1"] = {**whole, "trackCount": 10}
    short = await w.catalog.album(T + "al1")
    assert short.incomplete and len(short.tracks) == 4 and short.track_count == 10
    for tracks in (None, [], "none", [{"id": "x"}]):
        held.answers["album/al1"] = {**whole, "tracks": tracks}
        assert (await w.catalog.album(T + "al1")).incomplete
    assert not (await w.catalog.album(T + "al1")).numbered  # (its numbers are its list's order)


@pytest.mark.anyio
async def test_a_hostile_answer_is_read_within_bounds_or_refused() -> None:
    """Megabytes of text, thousands of items, numbers too large, nesting: what can be read
    is, cut to its bounds, and nothing else than a catalog error comes out."""
    w = world()
    held = w.server.held
    long = "a" + " " * 60_000 + "- x" + " " * 60_000 + "b"
    nested = [[[["x"]]], {"name": [1]}, "Y"]
    answer = {
        "tracks": [
            {"id": "h1", "title": long, "artist": long, "album": long, "duration": 3},
            {"id": 10**400, "title": "Huge ID", "duration": 3},
            {"id": "h2", "title": "Huge length", "duration": 10**400},
            {"id": "h3", "title": "Nested", "artist": nested, "duration": 3},
            {"id": "h4", "title": "Lone \ud800 one", "artist": "\udfff", "duration": 3},
            {"id": "\ud800", "title": "Not text", "duration": 3},
            {"id": "..", "title": "No segment", "duration": 3},
            *({"id": f"m{n}", "title": "Many", "duration": 3} for n in range(3000)),
        ],
        "albums": [{"id": "a", "title": long}]
        + [{"id": f"a{n}", "title": "A"} for n in range(3000)],
        "artists": [{"id": "r", "name": long}]
        + [{"id": f"r{n}", "name": "R"} for n in range(3000)],
    }
    held.answers["search"] = json.dumps(answer).encode()  # (within the answers' size limit)
    assert len(held.answers["search"]) < 1024 * 1024
    started = time.monotonic()
    found = await w.catalog.search("anything", 10_000)
    assert time.monotonic() - started < 2.0
    assert len(found.songs) <= 100 and len(found.albums) == 100 and len(found.artists) == 100
    first = found.songs[0]
    assert first.ref.id == T + "h1" and len(first.title) <= 500 and len(first.artist) <= 500
    # The items that cannot be read are left out, each alone: the others are there.
    assert [s.title for s in found.songs[:4]] == ["a", "Nested", "Lone ? one", "Many"]
    assert found.songs[1].artist == "Y"
    # (Text an answer can be written with: a lone surrogate is replaced.)
    assert json.dumps([s.title + s.artist for s in found.songs], ensure_ascii=False).encode()
    held.answers["album/al1"] = {
        "title": "Box",
        "tracks": [{"id": f"t{n}", "title": "T", "duration": 3} for n in range(3000)],
    }
    box = await w.catalog.album(T + "al1")
    assert len(box.tracks) == 500 and box.incomplete
    nested: Any = "x"
    for _ in range(3000):
        nested = [nested]
    held.answers["artist/ar1"] = {"name": "Deep", "albums": nested, "topTracks": {"a": nested}}
    page = await w.catalog.artist(I + "ar1")
    assert page.name == "Deep" and await w.catalog.artist_releases(I + "ar1") == ()


# --- artists are names --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_credit_is_an_artist_by_name_and_opens_with_one_search() -> None:
    w = world()
    release = await w.catalog.album(T + "al1")
    solo, featured = release.tracks[0], release.tracks[2]
    assert [r.id for r in solo.artist_refs] == ["nMara.20Venn"]
    assert release.artist_refs == solo.artist_refs
    assert [r.id for r in featured.artist_refs] == ["nMara.20Venn", "nOdile.20Brandt"]
    duo = (await w.catalog.album(T + "al3")).tracks[1]
    assert duo.artist == "Odile Brandt, Mara Venn"  # one item for the credit (not "feat.")
    assert [r.id for r in duo.artist_refs] == ["nOdile.20Brandt.2C.20Mara.20Venn"]
    assert await w.catalog.artists_of(("t101",), ("al1",)) == {}  # nothing more to ask
    w.server.asked.clear()

    artist = await w.catalog.artist("nOdile.20Brandt")
    assert artist.name == "Odile Brandt" and artist.ref.id == I + "ar2"  # the add-on's own item
    assert w.server.asked == ["search", "artist/ar2"]
    await w.catalog.artist_releases("nOdile.20Brandt")
    await w.catalog.artist("nOdile.20Brandt")
    assert w.server.asked == ["search", "artist/ar2"]  # the name is known from here on
    # The credit of two: its first name's artist.
    assert (await w.catalog.artist(duo.artist_refs[0].id)).ref.id == I + "ar2"
    with pytest.raises(CatalogError) as failed:
        await w.catalog.artist("nNobody.20Of.20That.20Name")
    assert failed.value.kind == "not_found"


@pytest.mark.anyio
async def test_an_artist_a_search_named_opens_without_another_search() -> None:
    w = world()
    await w.catalog.search("mara", 5)
    assert (await w.catalog.top_songs("nMara.20Venn", 1))[0].title == "Salt Meadow"
    assert w.server.asked == ["manifest.json", "search", "artist/ar1"]


# --- one song, and its album ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_song_is_what_the_last_answer_showed() -> None:
    w = world()
    with pytest.raises(CatalogError) as failed:
        await w.catalog.song(T + "t102")  # no answer showed it: the protocol cannot ask
    assert failed.value.kind == "not_found" and w.server.asked == []
    await w.catalog.search("salt meadow", 5)
    song = await w.catalog.song(T + "t102")
    assert song.title == "Salt Meadow" and song.album is None and song.number == 0
    release = await w.catalog.album(T + "al1")
    song = await w.catalog.song(T + "t102")
    assert song.album == release.ref and song.number == 2  # as its album lists it
    w.catalog._found.cache_clear()  # a search answered anew: it knows the album now
    again = (await w.catalog.search("salt meadow", 5)).songs[0]
    assert again.album == release.ref and again.number == 2
    assert (await w.catalog.song(T + "t102")).album == release.ref
    assert await w.catalog.songs_by_isrc("ZZSHA0000102") == ()  # albums match by title


@pytest.mark.anyio
async def test_the_album_of_a_song_is_looked_for_when_it_is_added() -> None:
    w = world()
    held = w.server.held
    await w.catalog.search("two kettles", 5)
    # Two albums are called "North Window", and the search for the song's album lists the
    # other one first: the one by the song's artist is opened first, and lists the song.
    both = [held.listed(held.records[3]), held.listed(held.records[2])]
    held.answers["search"] = {"albums": both}
    w.server.asked.clear()
    found = await w.catalog.album_of(T + "t302")
    assert found is not None and found.id == T + "al3"
    assert w.server.asked == ["search", "album/al3"]
    assert (await w.catalog.song(T + "t302")).album == found  # known from here on
    w.server.asked.clear()
    assert (await w.catalog.album_of(T + "t302")) == found and w.server.asked == []
    assert await w.catalog.album_of(T + "t999") is None  # no answer showed that song
    del held.answers["search"]

    # A song no album of that title lists: not found, after three albums at most.
    held = w.server.held
    held.answers["search"] = {
        "tracks": [{"id": "t777", "title": "Stray", "artist": "Mara Venn", "album": "Glass Rivers",
                    "duration": 3}],
        "albums": [{"id": f"al{n}", "title": "Glass Rivers", "artist": "Mara Venn"}
                   for n in (1, 5, 6, 7, 8)],
    }  # fmt: skip
    w.catalog._found.cache_clear()
    await w.catalog.search("stray", 5)
    w.server.asked.clear()
    assert await w.catalog.album_of(T + "t777") is None
    assert w.server.count("album") == 3 and w.server.count("search") <= 1


@pytest.mark.anyio
async def test_the_album_lookup_has_one_time_for_all_its_requests_and_uses_the_cache() -> None:
    w = world(timeout=0.3)
    await w.catalog.search("two kettles", 5)
    opened: list[str] = []

    async def cached(album_id: str) -> Any:  # the cache's ``album``: what a commit writes
        opened.append(album_id)
        return await w.catalog.album(album_id)

    outer = CachedCatalog(w.catalog, ttl=60)
    assert await outer.album_of(T + "t302") is not None
    w.server.asked.clear()
    await outer.album(T + "al3")
    assert w.server.asked == []  # the answer the lookup checked is the one kept
    assert await w.catalog.album_of(T + "t302", cached) is not None and opened == []  # (known)
    # A lookup whose requests together do not end in time - each of them would - is a
    # catalog error, not a commit that hangs for a time for each request.
    held = w.server.held
    stray = {"id": "t777", "title": "Stray", "artist": "X", "album": "Lost", "duration": 3}
    albums = [{"id": f"lost{n}", "title": "Lost", "artist": "X"} for n in range(3)]
    held.answers["search"] = {"tracks": [stray], "albums": albums}
    for n in range(3):
        held.answers[f"album/lost{n}"] = {"title": "Lost", "tracks": []}
    w.catalog._found.cache_clear()
    await w.catalog.search("stray", 5)
    w.server.delay["album"] = 0.2
    started = time.monotonic()
    with pytest.raises(CatalogError) as failed:
        await w.catalog.album_of(T + "t777")
    assert failed.value.kind == "unavailable" and "in time" in failed.value.reason
    assert 0.25 < time.monotonic() - started < 0.55 and w.server.count("album") <= 3


@pytest.mark.anyio
async def test_songs_shown_from_a_saved_list_are_known_again() -> None:
    """A saved list of top songs, shown after a restart (the cache's ``remember``): its
    songs can be played and added, though no answer of this run showed them."""
    before = world()
    saved = await before.catalog.top_songs(I + "ar1", 2)
    w = world()
    outer = CachedCatalog(w.catalog, ttl=60)
    outer.remember(saved)
    assert await outer.song(saved[0].ref.id) == saved[0] and w.server.asked == []
    foreign = dataclasses.replace(saved[1], ref=CatalogRef("demo", "1"))
    w.catalog.remember([foreign])  # another catalog's song is not this one's
    with pytest.raises(CatalogError):
        await w.catalog.song("1")


# --- artwork ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_only_an_address_the_add_on_named_is_fetched() -> None:
    w = world()
    release = await w.catalog.album(T + "al1")
    template = release.artwork_template
    assert template is not None and template.startswith(f"{IMAGES}/al1/640x640.jpg#sh=")
    data, kind = await w.catalog.artwork(template)
    assert data == cover_image("blue").read_bytes() and kind == "image/jpeg"
    assert w.images.asked == [f"{IMAGES}/al1/640x640.jpg"]  # (the mark is not sent)
    for foreign in (
        f"{IMAGES}/al1/640x640.jpg",  # not marked
        f"{IMAGES}/other.jpg#{template.rpartition('#')[2]}",  # another address's mark
        template.replace("#sh=", "#sh=0"),
        "https://catalog-contract.invalid/300x300.jpg",
    ):
        with pytest.raises(CatalogError) as failed:
            await w.catalog.artwork(foreign)
        assert failed.value.kind == "invalid" and "://" not in str(failed.value)
    assert len(w.images.asked) == 1
    # Another catalog's mark (another add-on's name) is not this one's.
    assert unmarked(catalog_key("Other"), template) is None
    assert unmarked(w.catalog.key, template) == f"{IMAGES}/al1/640x640.jpg"


def test_an_artwork_address_is_an_http_address_without_placeholders() -> None:
    key = "addonx"
    assert artwork_address(key, None, "", 7, ["x"]) is None
    for bad in (
        "ftp://h.invalid/x.jpg",
        "//h.invalid/x.jpg",
        "x.jpg",
        "https://user:pw@h.invalid/x.jpg",
        "https://h.invalid/a b",
        "https://h.invalid/" + "x" * 2000,
        "javascript:alert(1)",
        "https:///x.jpg",
    ):
        assert artwork_address(key, bad) is None, bad
    made = artwork_address(key, "", "https://h.invalid/{w}x{h}/a.jpg#frag")
    assert made is not None and made.startswith("https://h.invalid/%7Bw%7Dx%7Bh%7D/a.jpg#sh=")
    assert "{" not in made and unmarked(key, made) == "https://h.invalid/%7Bw%7Dx%7Bh%7D/a.jpg"
    assert artwork_address(key, None, "http://h.invalid/cover.png") is not None  # the next name's


@pytest.mark.anyio
async def test_artwork_failures_are_catalog_errors() -> None:
    w = world()
    template = (await w.catalog.album(T + "al1")).artwork_template
    assert template is not None
    for status, kind in ((404, "not_found"), (410, "not_found"), (429, "rate_limited"),
                         (500, "unavailable"), (302, "unavailable")):  # fmt: skip
        w.images.answer = httpx.Response(status)
        with pytest.raises(CatalogError) as failed:
            await w.catalog.artwork(template)
        assert failed.value.kind == kind and "://" not in str(failed.value)
    w.images.answer = httpx.Response(200, content=b"x" * (MAX_ARTWORK_BYTES + 1))
    with pytest.raises(CatalogError) as failed:
        await w.catalog.artwork(template)
    assert failed.value.kind == "invalid" and "too large" in failed.value.reason


@pytest.mark.anyio
async def test_a_cover_on_a_private_address_is_not_fetched() -> None:
    """Covers are fetched with a client of their own, public addresses only - whatever
    the add-on's reach."""
    w = world()
    w.server.held.images = "http://127.0.0.1:9/img"
    await w.catalog.http.aclose()
    w.catalog.http = plugin.Context().http()
    template = (await w.catalog.album(T + "al1")).artwork_template
    assert template is not None
    with pytest.raises(CatalogError) as failed:
        await w.catalog.artwork(template)
    assert failed.value.kind == "invalid" and "network policy" in failed.value.reason
    await w.catalog.aclose()


@pytest.mark.anyio
async def test_a_cover_the_add_on_serves_itself_takes_a_turn_at_its_limit() -> None:
    w = world(limits=Limits(50.0, 4, 0))
    assert w.pace is not None
    w.server.held.images = f"{BASE}/img"
    template = (await w.catalog.album(T + "al1")).artwork_template
    assert template is not None
    before = w.pace.sent
    await w.catalog.artwork(template)
    assert w.pace.sent == before + 1
    other = artwork_address(w.catalog.key, f"{IMAGES}/x.jpg")  # an image host: no turn
    assert other is not None
    await w.catalog.artwork(other)
    assert w.pace.sent == before + 1
    # An image host that sends the request on to the add-on's own address: a turn there.
    w.images.answer = None
    redirected = artwork_address(w.catalog.key, f"{IMAGES}/moved.jpg")
    assert redirected is not None

    async def handle(request: httpx.Request) -> httpx.Response:
        w.images.asked.append(str(request.url))
        if request.url.path.endswith("/moved.jpg"):
            return httpx.Response(302, headers={"location": f"{BASE}/img/there.jpg"})
        if request.url.path.endswith("/limited.jpg"):
            return httpx.Response(429, headers={"retry-after": "40"})
        return httpx.Response(200, content=cover_image("blue").read_bytes())

    await w.catalog.http.aclose()
    w.catalog.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    await w.catalog.artwork(redirected)
    assert w.pace.sent == before + 2 and w.images.asked[-1] == f"{BASE}/img/there.jpg"
    # Its "too many requests" to a cover leaves the add-on alone, for the time it names.
    limited = artwork_address(w.catalog.key, f"{BASE}/img/limited.jpg")
    assert limited is not None
    with pytest.raises(CatalogError) as failed:
        await w.catalog.artwork(limited)
    assert failed.value.kind == "rate_limited" and 39 < w.pace.blocked <= 40
    asked = len(w.images.asked)
    for address in (template, redirected):  # ... its covers too, also behind a redirect
        with pytest.raises(CatalogError) as failed:
            await w.catalog.artwork(address)
        assert failed.value.kind == "rate_limited"
    assert len(w.images.asked) == asked + 1  # (the image host's redirect, no more)
    with pytest.raises(CatalogError):
        await w.catalog.album(T + "al2")  # and its catalog requests


@pytest.mark.anyio
async def test_a_cover_at_an_add_on_on_a_private_address_takes_no_turn() -> None:
    """Covers are fetched from public addresses only: one the add-on would serve from this
    machine or network is refused before a turn at its limit is spent on it."""
    w = world(limits=Limits(50.0, 4, 0))
    assert w.pace is not None
    w.server.held.images = f"{BASE}/img"
    w.hosts["addon.example.invalid"] = ["10.1.2.3"]
    template = (await w.catalog.album(T + "al1")).artwork_template
    assert template is not None
    before = w.pace.sent
    with pytest.raises(CatalogError) as failed:
        await w.catalog.artwork(template)
    assert failed.value.kind == "invalid" and "network policy" in failed.value.reason
    assert w.pace.sent == before and w.images.asked == []


# --- failures -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_add_on_without_a_catalog_is_refused_with_the_reason() -> None:
    for resources in (("stream", "isrc"), ("stream", "search"), ("stream", "catalog")):
        w = world(resources=resources)
        for ask in (
            lambda w=w: w.catalog.search("venn", 5),
            lambda w=w: w.catalog.album(T + "al1"),
            lambda w=w: w.catalog.artist(I + "ar1"),
            lambda w=w: w.catalog.check(),
        ):
            with pytest.raises(CatalogError) as failed:
                await ask()
            assert failed.value.kind == "invalid" and failed.value.reason == NO_CATALOG
        assert w.server.count("search") == w.server.count("album") == 0  # never asked


@pytest.mark.anyio
async def test_the_add_on_named_must_be_enabled() -> None:
    w = world()
    w.registry.sources[0].name = "Renamed"
    with pytest.raises(CatalogError) as failed:
        await w.catalog.search("venn", 5)
    assert failed.value.kind == "unavailable" and failed.value.reason == NOT_ENABLED
    nowhere = AddonCatalog(NAME, None, httpx.AsyncClient())
    with pytest.raises(CatalogError) as failed:
        await nowhere.search("venn", 5)
    assert failed.value.kind == "unavailable" and w.server.asked == []
    await nowhere.aclose()


@pytest.mark.anyio
async def test_the_add_on_s_failures_are_the_catalog_s() -> None:
    w = world()
    await w.catalog.check()  # (its manifest is read)
    for status, kind in ((500, "unavailable"), (503, "unavailable"), (401, "unauthorized"),
                         (403, "unauthorized"), (404, "not_found"), (410, "not_found"),
                         (400, "invalid")):  # fmt: skip
        w.server.status["album"] = httpx.Response(status)
        with pytest.raises(CatalogError) as failed:
            await w.catalog.album(T + "al1")
        assert failed.value.kind == kind, status
        assert "://" not in str(failed.value) and "example" not in str(failed.value)
    w.server.status["album"] = httpx.Response(200, content=b"<html>")
    with pytest.raises(CatalogError) as failed:
        await w.catalog.album(T + "al1")
    assert failed.value.kind == "invalid"
    # Failures are not kept: the next request asks again.
    w.server.status["search"] = httpx.Response(502)
    with pytest.raises(CatalogError):
        await w.catalog.search("venn", 5)
    del w.server.status["search"]
    assert (await w.catalog.search("venn", 5)).songs


@pytest.mark.anyio
async def test_an_add_on_cooling_down_or_left_alone_is_not_asked() -> None:
    w = world(limits=Limits(50.0, 4, 0))
    assert w.pace is not None
    await w.catalog.search("venn", 5)
    asked = len(w.server.asked)
    w.registry.cooled.add(1)  # the routing passes it over (errors in a row)
    with pytest.raises(CatalogError) as failed:
        await w.catalog.album(T + "al1")
    assert failed.value.kind == "unavailable" and "cooling down" in failed.value.reason
    w.registry.cooled.clear()
    # "Too many requests": the add-on is left alone - the catalog too, as its lookups are.
    w.server.status["album"] = httpx.Response(429, headers={"retry-after": "40"})
    with pytest.raises(CatalogError) as failed:
        await w.catalog.album(T + "al1")
    assert failed.value.kind == "rate_limited" and 39 < w.pace.blocked <= 40
    del w.server.status["album"]
    asked = len(w.server.asked)
    for ask in (lambda: w.catalog.album(T + "al1"), lambda: w.catalog.artist(I + "ar1"),
                lambda: w.catalog.check()):  # fmt: skip
        with pytest.raises(CatalogError) as failed:
            await ask()
        assert failed.value.kind == "rate_limited"
    assert len(w.server.asked) == asked  # nothing was sent


@pytest.mark.anyio
async def test_a_slow_add_on_is_unavailable_after_the_catalog_s_timeout() -> None:
    w = world(timeout=0.2)
    await w.catalog.check()
    w.server.wait = anyio.Event()
    with pytest.raises(CatalogError) as failed:
        await w.catalog.album(T + "al1")
    assert failed.value.kind == "unavailable" and failed.value.reason == "no answer in time"
    w.server.wait.set()


# --- the request limit shared with audio ---------------------------------------------------------


@pytest.mark.anyio
async def test_catalog_requests_take_turns_at_the_add_on_s_limit_behind_the_play() -> None:
    w = world(limits=Limits(20.0, 1, 0))
    assert w.pace is not None
    await w.catalog.check()
    sent = w.pace.sent
    order: list[str] = []

    async def view(what: str) -> None:
        await w.catalog.album(what)
        order.append(f"view {what}")

    async def play() -> None:
        with pacing.urgent(pacing.PLAY):
            await w.pace.request()  # a lookup of the song being played
        order.append("play")

    async def background() -> None:
        with pacing.urgent(pacing.WARM):  # e.g. the library pass
            await w.catalog.album(T + "al3")
        order.append("background")

    async with anyio.create_task_group() as tg:
        await w.pace.request()  # the add-on's one request at once is taken
        tg.start_soon(background)
        await anyio.sleep(0.005)
        tg.start_soon(view, T + "al1")
        await anyio.sleep(0.005)
        tg.start_soon(view, T + "al2")
        await anyio.sleep(0.005)
        tg.start_soon(play)  # the last to come goes first
    assert order == ["play", f"view {T}al1", f"view {T}al2", "background"]
    assert w.pace.sent == sent + 5  # each catalog request took a turn, like a lookup


@pytest.mark.anyio
async def test_the_library_pass_s_requests_are_background_work() -> None:
    """Asked through the paced catalog (the library pass), a request waits behind the
    listeners' at the add-on's limit."""
    w = world()
    seen: list[int | None] = []

    async def album(addon: Addon) -> Any:
        current = pacing.current()
        seen.append(current.level if current is not None else None)
        return w.server.held.album("al1")

    paced = PacedCatalog(w.catalog, 1000.0)
    w.registry.sources[0].addon.album = lambda own: album(w.registry.sources[0].addon)  # type: ignore[method-assign]
    await paced.album(T + "al1")
    await w.catalog.album(T + "al1")
    assert seen == [pacing.WARM, pacing.QUEUED]


@pytest.mark.anyio
async def test_a_request_still_waiting_for_its_turn_says_so() -> None:
    w = world(limits=Limits(0.5, 1, 0), timeout=0.3)
    assert w.pace is not None
    await w.catalog.check()  # (the one request at once)
    with pytest.raises(CatalogError) as failed:
        await w.catalog.album(T + "al1")
    assert failed.value.kind == "unavailable" and failed.value.reason == pacing.REQUESTS
    assert w.server.count("album") == 0


# --- the kind's declaration ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_kind_is_built_in_and_needs_the_add_on_s_name() -> None:
    assert KIND in plugin.installed() and plugin.adapter(KIND) is adapter
    settings = load_settings(None, catalog={"kind": KIND, "addon": "Fake"})
    built = build_catalog(settings.catalog, addons=None)
    assert isinstance(built, AddonCatalog)
    assert built.key == catalog_key("Fake") and built.region == "" and built.name == "Fake"
    await built.aclose()
    for name in ("", "   "):
        unnamed = load_settings(None, catalog={"kind": KIND, "addon": name})
        found = adapter.problem(unnamed.catalog) if adapter.problem else None
        assert found is not None and found.setting == "addon"
        with pytest.raises(ValueError, match="needs \\[catalog\\] addon"):
            build_catalog(unnamed.catalog)


@pytest.mark.anyio
async def test_the_check_reads_the_manifest_again() -> None:
    w = world()
    await w.catalog.search("venn", 5)
    await w.catalog.check()
    await w.catalog.check()
    assert w.server.count("manifest.json") == 3
    w.server.resources = ("stream",)  # the add-on changed: its check says so
    with pytest.raises(CatalogError) as failed:
        await w.catalog.check()
    assert failed.value.reason == NO_CATALOG
