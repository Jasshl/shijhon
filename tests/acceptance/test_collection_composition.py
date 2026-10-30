"""Complete album pages and selected library lists through the production stack."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from defusedxml import ElementTree

from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    artist_data,
    release_data,
)
from shijhon.collection.contracts import Query
from shijhon.proxy import xmlform
from shijhon.proxy.app import Reply
from shijhon.views.answers import LibraryAnswer
from shijhon.views.ids import album_id as catalog_album_id
from shijhon.views.ids import artist_id as catalog_artist_id
from shijhon_demo_catalog import DemoCatalog
from tests.acceptance.test_I_fill import NS, OPENED
from tests.conftest import NavidromeFactory
from tests.harness import xsd
from tests.harness.collection import prepare_collection, select_catalog
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, simple_album, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay
from tests.harness.subsonic import SubsonicClient


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_artist_discography_keeps_unselected_album_after_materialization(
    navidrome_factory: NavidromeFactory, tmp_path: Path, fmt: str
) -> None:
    artist = CatalogArtist(CatalogRef("demo", "discovery-artist"), "Ink Cartographer")

    def release(key: str, title: str, count: int, kind: ReleaseKind) -> CatalogRelease:
        ref = CatalogRef("demo", key)
        return CatalogRelease(
            ref,
            title,
            artist.name,
            kind,
            "2020-01-01",
            tuple(
                CatalogTrack(
                    CatalogRef("demo", f"{key}-{n}"),
                    f"Chapter {n}",
                    artist.name,
                    3000,
                    album=ref,
                    number=n,
                    artist_refs=(artist.ref,),
                )
                for n in range(1, count + 1)
            ),
            artist_refs=(artist.ref,),
        )

    album = release("discovery-album", "Paper Fragments", 8, ReleaseKind.ALBUM)
    single = release("discovery-single", "Loose Leaf", 1, ReleaseKind.SINGLE)
    catalog = DemoCatalog(
        [
            {"path": f"artists/{artist.ref.id}", "body": artist_data(artist)},
            {
                "path": f"artists/{artist.ref.id}/releases",
                "body": [release_data(album), release_data(single)],
            },
            *({"path": f"albums/{r.ref.id}", "body": release_data(r)} for r in (album, single)),
        ]
    )
    nd = navidrome_factory()
    with delivery_world(nd, tmp_path / "artist-discovery", catalog=catalog) as world:
        client = world.client()
        owner = nd.login()["id"]
        browser = Browser(world.server.base_url)
        try:
            assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        finally:
            browser.close()

        def page(ident: str) -> dict[str, Any]:
            answer = client.request("getArtist", {"id": ident}, fmt=fmt)
            assert answer.status_code == 200
            document = answer.json() if fmt == "json" else xmlform.load(answer.content)
            assert document["subsonic-response"]["status"] == "ok"
            return document["subsonic-response"]["artist"]

        discovery = catalog_artist_id(artist.ref)
        assert {a["name"] for a in page(discovery)["album"]} == {album.title, single.title}
        # First use publishes the album globally; it does not enable its tracks
        # in this account's library lists or shuffle.
        materialized = world.materialize(album)
        collection = world.services.collection
        assert collection is not None

        async def refresh() -> None:
            await collection.refresh(owner, "1")

        world.server.call(refresh)
        native = nd.client()
        try:
            detail = native.ok("getAlbum", {"id": materialized.album_id})["album"]
            native_artist = detail["artistId"]
            files = {p: p.read_bytes() for p in world.placeholder_files()}
            for ident in (native_artist, discovery):
                albums = page(ident)["album"]
                assert len(albums) == 2
                assert {a["name"] for a in albums} == {album.title, single.title}
                full = next(a for a in albums if a["name"] == album.title)
                assert full["id"] == materialized.album_id and int(full["songCount"]) == 8
            assert {p: p.read_bytes() for p in world.placeholder_files()} == files
            assert not client.ok("getAlbumList2", {"type": "alphabeticalByName"})["albumList2"].get(
                "album"
            )
            assert not client.ok("getRandomSongs", {"size": 20})["randomSongs"].get("song")

            async def unchanged_choices() -> None:
                for query in (
                    "SELECT count(*) n FROM collection_membership",
                    "SELECT count(*) n FROM collection_history",
                ):
                    row = await world.services.store.fetchone(query)
                    assert row is not None and row["n"] == 0

            world.server.call(unchanged_choices)
        finally:
            native.close()
            client.close()


def test_partial_album_opens_complete_before_and_after_fill_without_enabling_tracks(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
) -> None:
    nd = navidrome_factory()
    write_album(nd.music, OPENED)
    nd.scan(full=True)
    replay = Replay()
    replay.aliases["neve ashdown northern letters (remastered)"] = "tender rivers"
    native = nd.client()
    album = native.ok("search3", {"query": OPENED.title, "artistCount": 0, "songCount": 0})[
        "searchResult3"
    ]["album"][0]["id"]
    original = native.ok("getAlbum", {"id": album})["album"]
    with delivery_world(
        nd,
        tmp_path / "full-stack",
        catalog=replay.catalog(),
        fill={
            "enabled": True,
            "auto_min_songs": 50,
            "library_pass": "off",
            "open_budget_seconds": 15,
        },
    ) as world:
        client = world.client()
        owner = nd.login()["id"]
        browser = Browser(world.server.base_url)
        try:
            assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        finally:
            browser.close()
        collection = world.services.collection
        assert collection is not None

        async def refresh() -> None:
            await collection.refresh(owner, "1")

        world.server.call(refresh)
        for fmt in ("json", "xml"):
            answer = client.request("getAlbum", {"id": album}, fmt=fmt)
            assert answer.status_code == 200
            if fmt == "json":
                detail = answer.json()["subsonic-response"]["album"]
                assert detail["songCount"] == len(detail["song"]) == 17
                assert [s for s in detail["song"] if not s["id"].startswith("sh.")] == original[
                    "song"
                ]
                assert sum(s["id"].startswith("sh.tr.") for s in detail["song"]) == 15
                assert detail["duration"] > original["duration"]
            else:
                assert b'songCount="17"' in answer.content
            assert answer.headers["x-total-count"] == "17"

        async def cached_completion() -> None:
            assert world.services.fills is not None
            shown = await world.services.fills.shown([album])
            # Album detail and library lists use the complete plan until the fill,
            # and Navidrome's count of the whole album after it, while the
            # selection holds the two original tracks and no catalog songs.
            assert shown[album].owned == 2 and len(shown[album].missing) == 15

        world.server.call(cached_completion)

        async def matched() -> str:
            row = await world.services.store.fetchone(
                "SELECT release_ref FROM album_matches WHERE album_id=?", [album]
            )
            assert row is not None
            return catalog_album_id(CatalogRef.parse(row["release_ref"]))

        discovery = world.server.call(matched)
        assert client.ok("getAlbum", {"id": discovery})["album"]["songCount"] == 17
        listed = client.ok("getAlbumList2", {"type": "alphabeticalByName"})["albumList2"]["album"][
            0
        ]
        assert listed["songCount"] == 17 and listed["duration"] == detail["duration"]

        async def fill_and_refresh() -> None:
            assert world.services.fills is not None
            filled = await world.services.fills.fill_album(album, "explicit fixture fill")
            assert filled and filled.outcome == "filled"
            await collection.refresh(owner, "1")

        world.server.call(fill_and_refresh)
        assert native.ok("getAlbum", {"id": album})["album"]["songCount"] == 17
        assert client.ok("getAlbum", {"id": discovery})["album"]["songCount"] == 17
        detail = client.ok("getAlbum", {"id": album})["album"]
        assert detail["songCount"] == len(detail["song"]) == 17
        assert [s["id"] for s in detail["song"]] == [
            s["id"] for s in native.ok("getAlbum", {"id": album})["album"]["song"]
        ]
        listed = client.ok("getAlbumList2", {"type": "alphabeticalByName"})["albumList2"]["album"][
            0
        ]
        own = native.ok("getAlbumList2", {"type": "alphabeticalByName"})["albumList2"]["album"][0]
        # All holds the whole album of the account's original songs, its placeholders
        # included, without any choice made for them.
        assert own["songCount"] == 17
        assert (listed["songCount"], listed["duration"]) == (17, own["duration"])
        selected = client.ok(
            "search3", {"query": "", "artistCount": 0, "albumCount": 0, "songCount": 500}
        )["searchResult3"]["song"]
        assert {song["id"] for song in selected} == {song["id"] for song in detail["song"]}

        async def unchanged_choices() -> None:
            row = await world.services.store.fetchone(
                "SELECT count(*) n FROM collection_membership"
            )
            assert row is not None and row["n"] == 0
            assert (await collection.profile(owner))["selection_id"] == "all"

        world.server.call(unchanged_choices)


LIBRARY_LISTS = (
    ("getAlbumList2", {"type": "alphabeticalByName"}, "albumList2"),
    ("search3", {"query": "", "artistCount": 0, "songCount": 0}, "searchResult3"),
)


def library_counts(client: SubsonicClient, fmt: str) -> list[dict[str, tuple[int, int]]]:
    """(songCount, duration) of each album in the library lists (``getAlbumList2``, a
    ``search3`` sync) in ``fmt`` (JSON, XML or JSONP). ``X-Total-Count`` counts albums."""
    found = []
    for method, params, key in LIBRARY_LISTS:
        if fmt == "xml":
            answer = client.request(method, params, fmt="xml")
            assert answer.status_code == 200 and xsd.valid(answer.content)
            root = ElementTree.fromstring(answer.content)
            albums = [dict(e.attrib) for e in root.iter(f"{NS}album")]
        else:
            asked = {**params, "f": "jsonp", "callback": "cb"} if fmt == "jsonp" else params
            answer = client.request(method, asked, fmt="xml" if fmt == "jsonp" else "json")
            assert answer.status_code == 200
            body = answer.content[3:-1] if fmt == "jsonp" else answer.content
            albums = json.loads(body)["subsonic-response"][key].get("album", [])
        if method == "getAlbumList2":
            assert answer.headers["x-total-count"] == str(len(albums))
        found.append({str(a["id"]): (int(a["songCount"]), int(a["duration"])) for a in albums})
    return found


@dataclass
class PartlyOwned:
    world: DeliveryWorld
    client: SubsonicClient
    owner: str
    album: str  # two songs of a 17-track release, shown complete
    other: str  # an album the catalog has no release for
    original: dict[str, Any]  # the album as Navidrome has it
    companion: dict[str, Any]
    complete: dict[str, Any]  # the album shown complete


@contextmanager
def partly_owned(navidrome_factory: NavidromeFactory, tmp: Path) -> Iterator[PartlyOwned]:
    """A partly owned album with a saved plan, and an album without one."""
    nd = navidrome_factory()
    write_album(nd.music, OPENED)
    planless = simple_album("Invented Lister", "Planless Companion", 3, seconds=5.0)
    write_album(nd.music, planless)
    nd.scan(full=True)
    replay = Replay()
    replay.aliases["neve ashdown northern letters (remastered)"] = "tender rivers"
    native = nd.client()
    listed = native.ok("getAlbumList2", {"type": "alphabeticalByName"})["albumList2"]["album"]
    ids = {entry["name"]: entry["id"] for entry in listed}
    album, other = ids[OPENED.title], ids[planless.title]
    original = native.ok("getAlbum", {"id": album})["album"]
    companion = native.ok("getAlbum", {"id": other})["album"]
    fill = {"enabled": True, "auto_min_songs": 50, "library_pass": "off",
            "open_budget_seconds": 15}  # fmt: skip
    with delivery_world(nd, tmp, catalog=replay.catalog(), fill=fill) as world:
        client = world.client()
        owner = prepare_collection(nd, world.server)
        complete = client.ok("getAlbum", {"id": album})["album"]  # the plan is saved
        assert complete["songCount"] == len(complete["song"]) == 17
        try:
            yield PartlyOwned(world, client, owner, album, other, original, companion, complete)
        finally:
            client.close()


def choose_in(
    world: DeliveryWorld,
    owner: str,
    profile: str,
    refs: list[str],
    *,
    keys: tuple[str, ...] | list[str] = (),
    action: str = "save",
) -> None:
    """Add these catalog songs (or remove these songs, ``keys``) by hand in ``profile``."""
    collection = world.services.collection
    assert collection is not None

    async def choose() -> None:
        assert collection is not None
        operation = await collection.prepare(
            owner,
            "1",
            Query(view="all", scope="song", origin="catalog" if refs else "all"),
            action=action,
            selected=["catalog:" + ref for ref in refs] or list(keys),
            request_key=f"fixture-{action}-{profile}",
            profile_id=profile,
        )
        await collection.tick()
        await collection.approve(owner, operation)
        for _ in range(10):
            await collection.tick()
            if (await collection.operation(owner, operation))["state"] == "done":
                return
        raise AssertionError(await collection.operation(owner, operation))

    world.server.call(choose)


def leave_out(world: DeliveryWorld, owner: str, song: str) -> None:
    """Activate a custom library copied from Original imported library without ``song``."""
    collection = world.services.collection
    assert collection is not None

    async def custom() -> None:
        profile = await collection.create_profile(owner, "Without one song", start="original")
        await collection.activate_profile(owner, profile)
        operation = await collection.prepare(
            owner,
            "1",
            Query(view="all", scope="song"),
            action="remove",
            selected=["native:" + song],
            request_key="fixture-without-" + song,
        )
        await collection.tick()
        frozen = await collection.operation(owner, operation)
        assert frozen["state"] == "review" and frozen["total"] == 1, frozen
        await collection.approve(owner, operation)
        for _ in range(10):
            await collection.tick()
            if (await collection.operation(owner, operation))["state"] == "done":
                break
        assert (await collection.operation(owner, operation))["state"] == "done"

    world.server.call(custom)


def activate(world: DeliveryWorld, owner: str, profile: str) -> None:
    collection = world.services.collection
    assert collection is not None

    async def active() -> None:
        await collection.activate_profile(owner, profile)

    world.server.call(active)


def test_library_lists_show_complete_counts_only_when_every_owned_song_is_selected(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    """The library lists (``getAlbumList2``, a ``search3`` sync) of an album shown complete:
    its complete counts while the selection holds every owned song its plan links, its
    selected counts while it holds fewer; an album without a plan keeps its own. Which
    albums are listed and ``X-Total-Count`` stay the selection's; JSONP is the selected
    representation."""
    with partly_owned(navidrome_factory, tmp_path / "subset") as case:
        album, other, original = case.album, case.other, case.original
        whole = {album: (2, original["duration"]), other: (3, case.companion["duration"])}
        shown = {**whole, album: (17, case.complete["duration"])}
        assert library_counts(case.client, "jsonp") == [whole, whole]  # selected
        assert library_counts(case.client, "json") == [shown, shown]
        assert library_counts(case.client, "xml") == [shown, shown]
        leave_out(case.world, case.owner, original["song"][0]["id"])
        one = library_counts(case.client, "jsonp")[0][album]
        assert one[0] == 1 and one[1] < original["duration"]
        partial = {**whole, album: one}
        # One of the plan's two owned songs selected: the selected counts, in every format.
        for fmt in ("jsonp", "json", "xml"):
            assert library_counts(case.client, fmt) == [partial, partial], fmt
        assert case.client.ok("getAlbum", {"id": album})["album"]["songCount"] == 17
        activate(case.world, case.owner, "all")
        assert library_counts(case.client, "json") == [shown, shown]
        assert library_counts(case.client, "xml") == [shown, shown]


def test_a_plan_does_not_count_an_album_whose_new_song_is_left_out(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    """A song imported into the album after its plan, then left out of the library: the
    plan's two songs are listed, as many as it links, but the album is no longer the
    plan's, so the library lists keep the selected counts in every format."""
    with partly_owned(navidrome_factory, tmp_path / "imported") as case:
        nd = case.world.nd
        added = Album(OPENED.artist, OPENED.title, (Track("Invented Interlude", 3, seconds=40),),
                      recording_date=OPENED.recording_date)  # fmt: skip
        write_album(nd.music, added)
        nd.scan(targets=[OPENED.relative_folder])
        prepare_collection(nd, case.world.server)
        songs = nd.client().ok("getAlbum", {"id": case.album})["album"]["song"]
        planned = {song["id"] for song in case.original["song"]}
        [new] = [song["id"] for song in songs if song["id"] not in planned]
        assert len(songs) == 3
        assert library_counts(case.client, "json")[0][case.album][0] == 3  # all selected
        leave_out(case.world, case.owner, new)
        two = library_counts(case.client, "jsonp")
        assert two[0][case.album][0] == two[1][case.album][0] == 2
        assert library_counts(case.client, "json") == library_counts(case.client, "xml") == two


def test_a_filled_album_lists_navidrome_s_counts_while_its_own_songs_are_selected(
    navidrome_factory: NavidromeFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once filled, the album's placeholders are in Original imported library with its
    original songs (the whole album), and so in All. Removed from All by hand, they are
    songs Shijhon added, not selected: the library lists' JSON and XML GET count the album
    as Navidrome does (17) while the selection holds all of its original songs, also with
    some catalog songs added. A selection without one of them (a custom library, or
    Shijhon library holding only catalog songs) keeps its selected counts; HEAD and JSONP
    describe the selected lists."""
    with partly_owned(navidrome_factory, tmp_path / "filled") as case:
        album, other, original = case.album, case.other, case.original
        catalog = [
            f"demo:{song['id'].rsplit('.', 1)[1]}"
            for song in case.complete["song"]
            if song["id"].startswith("sh.tr.")
        ]
        assert len(catalog) == 15
        fills, collection = case.world.services.fills, case.world.services.collection
        assert fills is not None and collection is not None

        async def fill() -> None:
            assert fills is not None and collection is not None
            filled = await fills.fill_album(album, "explicit fixture fill")
            assert filled and filled.outcome == "filled"
            await collection.refresh(case.owner, "1")

        case.world.server.call(fill)
        native = case.world.nd.client()
        own = {
            a["id"]: (a["songCount"], a["duration"])
            for a in native.ok("getAlbumList2", {"type": "alphabeticalByName"})["albumList2"][
                "album"
            ]
        }
        assert own[album][0] == 17 and own[other] == (3, case.companion["duration"])
        # The whole album is selected: every list counts it as Navidrome does.
        for fmt in ("jsonp", "json", "xml"):
            assert library_counts(case.client, fmt) == [own, own], fmt
        select_catalog(case.world.nd, case.world.server, catalog, saved=False)
        selected = {album: (2, original["duration"]), other: own[other]}
        assert library_counts(case.client, "json") == [own, own]
        assert library_counts(case.client, "xml") == [own, own]
        assert library_counts(case.client, "jsonp") == [selected, selected]
        heads: list[bytes] = []
        reply = LibraryAnswer.reply

        def observed(answer: LibraryAnswer, **kwargs: Any) -> Reply:
            if kwargs.get("head"):
                heads.append(answer.body)
            return reply(answer, **kwargs)

        monkeypatch.setattr(LibraryAnswer, "reply", observed)
        head = case.client.request(
            "getAlbumList2",
            {"type": "alphabeticalByName"},
            http_method="HEAD",
            headers={"Accept-Encoding": "identity"},
        )
        assert head.status_code == 200 and head.content == b""
        assert head.headers["x-total-count"] == "2"
        [described] = heads
        assert head.headers["content-length"] == str(len(described))
        cards = json.loads(described)["subsonic-response"]["albumList2"]["album"]
        assert {a["id"]: (a["songCount"], a["duration"]) for a in cards} == selected
        monkeypatch.undo()

        # Three catalog songs added again: every original song is still selected.
        select_catalog(case.world.nd, case.world.server, catalog[:3])
        five = library_counts(case.client, "jsonp")[0][album]
        assert five[0] == 5
        assert library_counts(case.client, "json") == [own, own]
        assert library_counts(case.client, "xml") == [own, own]
        # Shijhon library: the three catalog songs, added there by hand (nothing of this
        # account's activity brings the album in; a song added brings its album, so the
        # album's other songs are removed there by hand), the original songs left out.
        choose_in(case.world, case.owner, "shijhon", catalog[:3])
        others = ["catalog:" + ref for ref in catalog[3:]]
        others += ["native:" + song["id"] for song in original["song"]]
        choose_in(case.world, case.owner, "shijhon", [], keys=others, action="remove")
        activate(case.world, case.owner, "shijhon")
        three = library_counts(case.client, "jsonp")[0][album]
        assert three[0] == 3 and three[1] < own[album][1]
        for fmt in ("jsonp", "json", "xml"):
            assert library_counts(case.client, fmt) == [{album: three}] * 2, fmt
        # A custom library copied from Original imported library (the whole album) without
        # one of the two original songs.
        leave_out(case.world, case.owner, original["song"][0]["id"])
        one = library_counts(case.client, "jsonp")[0][album]
        assert one[0] == 16 and one[1] < own[album][1]
        for fmt in ("jsonp", "json", "xml"):
            assert library_counts(case.client, fmt) == [{**selected, album: one}] * 2, fmt
        activate(case.world, case.owner, "all")
        assert library_counts(case.client, "json") == [own, own]
        detail = case.client.ok("getAlbum", {"id": album})["album"]
        assert detail["songCount"] == len(detail["song"]) == 17
