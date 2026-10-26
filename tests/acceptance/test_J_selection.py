"""Suite J - search with a personal selection, in JSON and XML.

A search lists the caller's selected songs and albums only: a placeholder that is not
enabled is not among them. A catalog song or album the library has is left out of the
additions only when the caller's selection shows that library copy; when it hides it, the
catalog entry stands for it - once - and opening or playing that entry uses the library
copy, adding nothing. Once enabled, the library copy shows alone. An account whose
selection shows the library copies gets the answers it got before; an account that gets
real audio only gets no catalog entries.

The library has two tracks of an album the catalog completes with placeholders, and a
catalog album added as an album of its own. The administrator's All holds neither's
placeholders: the completed album's are removed there explicitly (its original songs
bring it in whole otherwise), and nothing brings in the other album. The listener has all
of them enabled.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any, Literal

import pytest

from shijhon.catalog.model import release_from_data
from tests.conftest import NavidromeFactory
from tests.harness import xsd
from tests.harness.collection import prepare_collection, select_catalog
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient

NS = "{http://subsonic.org/restapi}"
PASSWORD = "invented-selection-password"
LISTENER, ANALYZER = "listener", "analyzer"
FILLED = "album-ep-without-suffix"  # "wild stones" (9 tracks): two owned, the rest filled
OWN = "album-anniversary-standard"  # "Salt 2": in the library as an album of its own
OWNED = Album(
    "Jonah Fairbanks",
    "wild stones",
    (Track("FROZEN", 1, isrc="ZZSHJ0000087"), Track("violet", 2, isrc="ZZSHJ0000088")),
    recording_date="2010",
)
SONG_TERM = "jonah fairbanks electric mirrors"  # a placeholder of the filled album
SONG = "electric mirrors"
SONG_REF = "900000138"
SONG_ISRC = "ZZSHJ0000089"
OWNED_REF = "900000136"  # "FROZEN": linked to the owned file, which every selection shows
ALBUM_TERM = "brokenmeadow salt 2"
ALBUM = "Salt 2"
ALBUM_REF = "900000203"
Format = Literal["json", "xml"]
FORMATS: tuple[Format, ...] = ("json", "xml")


def tracks(name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = fixture(name)["body"]["tracks"]
    return rows


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    replay.aliases[SONG_TERM] = str(fixture("search-ep")["params"]["term"])
    replay.aliases[ALBUM_TERM] = str(fixture("search-anniversary")["params"]["term"])
    return replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, OWNED)
    nd.scan(full=True)
    for user in (LISTENER, ANALYZER):
        nd.create_user(user, PASSWORD)
    found = nd.client().ok("search3", {"query": OWNED.title, "artistCount": 0, "songCount": 0})
    [album_id] = [a["id"] for a in found["searchResult3"]["album"]]
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("selection"),
        catalog=replay.catalog(),
        real_audio_accounts=[ANALYZER],
    ) as w:
        fill = release_from_data(fixture(FILLED)["body"])
        engine = w.services.engine

        async def filled() -> None:
            album = await engine.owned_album(album_id)
            links = {t.ref: album.songs[t.number - 1]["id"] for t in fill.tracks[:2]}
            await engine.materialize(fill, owned_album_id=album_id, links=links)

        w.server.call(filled)
        w.materialize(release_from_data(fixture(OWN)["body"]))
        addon = w.addon("Selection source")
        w.add_source(addon)
        addon.add(FakeTrack(isrc=SONG_ISRC, audio=w.audio("electric-mirrors")))
        prepare_collection(nd, w.server)  # the library index, with the placeholders
        filled_in = [f"demo:{t['ref']['id']}" for t in tracks(FILLED)[2:]]
        select_catalog(nd, w.server, filled_in, saved=False)
        own = [f"demo:{t['ref']['id']}" for t in tracks(OWN)]
        select_catalog(nd, w.server, filled_in + own, user=LISTENER, password=PASSWORD)
        yield w


@pytest.fixture
def client(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    c = world.client(client="selection-tests")
    yield c
    c.close()


def search(client: SubsonicClient, term: str, fmt: Format = "json") -> dict[str, list[Any]]:
    """The entries of a search3 answer by kind (XML: each element's attributes)."""
    if fmt == "json":
        found: dict[str, list[Any]] = client.ok("search3", {"query": term})["searchResult3"]
        return found
    answer = client.request("search3", {"query": term}, fmt="xml")
    assert answer.status_code == 200 and xsd.valid(answer.content)
    root = ET.fromstring(answer.content)  # noqa: S314 - test data
    result = root.find(NS + "searchResult3")
    assert result is not None
    return {
        kind: [dict(e.attrib) for e in result.findall(NS + kind)]
        for kind in ("artist", "album", "song")
    }


def songs_named(found: dict[str, list[Any]], title: str) -> list[str]:
    return [s["id"] for s in found.get("song", []) if s["title"] == title]


def albums_named(found: dict[str, list[Any]], name: str, artist: str) -> list[str]:
    return [a["id"] for a in found.get("album", []) if (a["name"], a["artist"]) == (name, artist)]


def library_rows(world: DeliveryWorld) -> dict[str, Any]:
    """What the library holds of the catalog: releases, placeholders and links."""

    async def read() -> dict[str, Any]:
        store = world.services.store
        return {
            table: [
                tuple(r)
                for r in await store.fetchall(f"SELECT * FROM {table} ORDER BY 1")  # noqa: S608
            ]
            for table in ("releases", "track_links")  # fixed tables
        } | {
            "placeholders": [
                tuple(r)
                for r in await store.fetchall(
                    "SELECT song_id, path, track_ref, release_ref FROM placeholders ORDER BY 1"
                )
            ]
        }

    return world.server.call(read)


def linked(world: DeliveryWorld, ref: str) -> str:
    async def read() -> str:
        row = await world.services.store.fetchone(
            "SELECT song_id FROM track_links WHERE track_ref = ?", [f"demo:{ref}"]
        )
        assert row is not None
        return str(row["song_id"])

    return world.server.call(read)


def library_album(world: DeliveryWorld, ref: str) -> str:
    async def read() -> str:
        row = await world.services.store.fetchone(
            "SELECT album_id FROM releases WHERE ref = ?", [f"demo:{ref}"]
        )
        assert row is not None
        return str(row["album_id"])

    return world.server.call(read)


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_placeholder_the_selection_hides_is_found_once_as_its_catalog_song(
    client: SubsonicClient, fmt: Format
) -> None:
    found = search(client, SONG_TERM, fmt)
    assert songs_named(found, SONG) == [f"sh.tr.demo.{SONG_REF}"]
    # A catalog song whose library copy the selection shows (an owned file) stays out.
    assert f"sh.tr.demo.{OWNED_REF}" not in [s["id"] for s in found["song"]]


def test_the_catalog_song_opens_and_plays_the_library_s_placeholder(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    [entry] = songs_named(search(client, SONG_TERM), SONG)
    placeholder = linked(world, SONG_REF)
    before = library_rows(world)
    assert client.ok("getSong", {"id": entry})["song"]["id"] == placeholder
    played = client.request("stream", {"id": entry})
    assert played.status_code == 200 and played.content[:4] == b"fLaC"
    client.ok("scrobble", {"id": entry})
    client.ok("star", {"id": entry})
    starred = client.ok("getStarred2")["starred2"]
    assert placeholder in [s["id"] for s in starred.get("song", [])]
    assert library_rows(world) == before  # nothing added: no second release or copy
    # Played and starred, still removed explicitly: the catalog entry stands for it.
    assert songs_named(search(client, SONG_TERM), SONG) == [entry]


@pytest.mark.parametrize("fmt", FORMATS)
def test_an_album_none_of_whose_songs_are_enabled_is_found_once_as_its_catalog_album(
    client: SubsonicClient, fmt: Format
) -> None:
    found = search(client, ALBUM_TERM, fmt)
    assert albums_named(found, ALBUM, "BrokenMeadow") == [f"sh.al.demo.{ALBUM_REF}"]


def test_the_catalog_album_opens_as_the_library_s_album(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    [entry] = albums_named(search(client, ALBUM_TERM), ALBUM, "BrokenMeadow")
    native = library_album(world, ALBUM_REF)
    before = library_rows(world)
    album = client.ok("getAlbum", {"id": entry})["album"]
    assert album["id"] == native and album["songCount"] == len(tracks(OWN))
    assert {s["id"] for s in album["song"]} == {linked(world, t["ref"]["id"]) for t in tracks(OWN)}
    client.ok("star", {"albumId": entry})
    starred = client.ok("getStarred2")["starred2"]
    assert native in [a["id"] for a in starred.get("album", [])]
    assert library_rows(world) == before


def test_once_enabled_the_library_s_song_shows_alone(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    select_catalog(world.nd, world.server, [f"demo:{SONG_REF}"])
    for fmt in FORMATS:
        found = search(client, SONG_TERM, fmt)
        assert songs_named(found, SONG) == [linked(world, SONG_REF)], fmt


def test_once_one_of_its_songs_is_enabled_the_library_s_album_shows_alone(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    select_catalog(world.nd, world.server, [f"demo:{tracks(OWN)[0]['ref']['id']}"])
    for fmt in FORMATS:
        found = search(client, ALBUM_TERM, fmt)
        assert albums_named(found, ALBUM, "BrokenMeadow") == [library_album(world, ALBUM_REF)]


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_selection_that_shows_the_library_copies_gets_no_catalog_copy_of_them(
    world: DeliveryWorld, fmt: Format
) -> None:
    listener = SubsonicClient(world.server.base_url, LISTENER, PASSWORD)
    try:
        songs = search(listener, SONG_TERM, fmt)
        albums = search(listener, ALBUM_TERM, fmt)
    finally:
        listener.close()
    assert songs_named(songs, SONG) == [linked(world, SONG_REF)]
    assert albums_named(albums, ALBUM, "BrokenMeadow") == [library_album(world, ALBUM_REF)]
    rows = library_rows(world)
    in_library = {f"sh.tr.{ref.replace(':', '.')}" for ref, *_ in rows["track_links"]}
    in_library |= {f"sh.al.{ref.replace(':', '.')}" for ref, *_ in rows["releases"]}
    shown = [e["id"] for found in (songs, albums) for kind in found.values() for e in kind]
    assert any(i.startswith("sh.") for i in shown)  # the catalog's other entries
    assert not in_library & set(shown)


def test_an_account_that_gets_real_audio_only_gets_no_catalog_entries(
    world: DeliveryWorld,
) -> None:
    analyzer = SubsonicClient(world.server.base_url, ANALYZER, PASSWORD)
    try:
        for term in (SONG_TERM, ALBUM_TERM):
            for fmt in FORMATS:
                found = search(analyzer, term, fmt)
                ids = [e["id"] for kind in found.values() for e in kind]
                assert not [i for i in ids if i.startswith("sh.")], (term, fmt)
                assert linked(world, SONG_REF) not in ids
    finally:
        analyzer.close()
