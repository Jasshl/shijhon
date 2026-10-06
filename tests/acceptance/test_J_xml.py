"""Suites G and J in XML - every answer Shijhon produces or extends, also in XML.

Clients that read the Subsonic API as XML only get the same answers as JSON
clients: catalog entries in search3, getAlbum, getSong, getArtist, getArtistInfo(2),
getAlbumInfo(2) and getMusicDirectory, library artists' pages with their catalog
releases, and errors. For each of them:

- the XML answer is valid against ``subsonic-rest-api.xsd`` (1.16.1) with the OpenSubsonic
  additions Navidrome emits (``tests/harness/xsd.py``; Navidrome's own answers validate too);
- it holds the same data as the JSON answer, entry by entry: every non-empty JSON value is
  an attribute (or a text element) with the same value, every list an element per item,
  nothing else (empty values are left out, as Navidrome leaves them out in XML) - checked
  here independently of Shijhon's own XML writer, and on Navidrome's own answers too;
- Navidrome's own entries in an answer Shijhon extends are Navidrome's elements as it wrote
  them, and Navidrome's answers read and written back are byte for byte the same;
- its Content-Type is Navidrome's.
"""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from shijhon.proxy import xmlform
from tests.acceptance.test_J_contract import ARTIST, OWNED, TERM, item
from tests.conftest import NavidromeFactory
from tests.harness import xsd
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import write_album
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient
from tests.harness.xsd import local, same_data

HOST = {"host": "music.test"}
NS = "{http://subsonic.org/restapi}"


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    replay.aliases[TERM] = str(fixture("search-single-vs-album")["params"]["term"])
    replay.aliases["wren loring"] = str(fixture("search-covers")["params"]["term"])
    return replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    for album in OWNED:
        write_album(nd.music, album)
    nd.scan(full=True)
    admin = nd.client()
    found = admin.ok("search3", {"query": "Hollow 2", "artistCount": 0})["searchResult3"]
    admin.ok("star", {"id": found["song"][0]["id"]})  # listener's marks in the own entries
    admin.ok("star", {"albumId": found["album"][0]["id"]})
    admin.ok("setRating", {"id": found["song"][0]["id"], "rating": 4})
    with delivery_world(nd, tmp_path_factory.mktemp("xml"), catalog=replay.catalog()) as w:
        yield w


@pytest.fixture
def client(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    c = world.client(headers=HOST, client="XmlClient")
    yield c
    c.close()


def navidrome(world: DeliveryWorld) -> SubsonicClient:
    return world.nd.client(headers=HOST, client="XmlClient")


def both(
    client: SubsonicClient, method: str, params: dict[str, Any]
) -> tuple[dict[str, Any], httpx.Response]:
    """The JSON answer's ``subsonic-response`` and the XML answer, checked: valid against the
    schema, the same data, Navidrome's Content-Type."""
    as_json = client.request(method, params, fmt="json").json()["subsonic-response"]
    as_xml = client.request(method, params, fmt="xml")
    assert as_xml.status_code == 200, method
    assert as_xml.headers["content-type"] == "application/xml", method
    assert xsd.valid(as_xml.content)
    root = ET.fromstring(as_xml.content)  # noqa: S314 - test data
    assert root.tag == NS + "subsonic-response"
    assert same_data(as_json, root, method) == []
    return as_json, as_xml


def entries(response: httpx.Response, path: str) -> list[ET.Element]:
    root = ET.fromstring(response.content)  # noqa: S314 - test data
    return root.findall("/".join(NS + part for part in path.split("/")))


def canonical(element: ET.Element) -> bytes:
    """An element as text, its roles sorted (Navidrome's order of them changes)."""
    element = copy.deepcopy(element)
    for holder in element.iter():
        roles = [c for c in holder if local(c.tag) == "roles"]
        for role in roles:
            holder.remove(role)
        for role in sorted(roles, key=lambda r: r.text or ""):
            holder.append(role)
    return ET.tostring(element)


def own_elements_as_navidrome_wrote_them(
    ours: httpx.Response, theirs: httpx.Response, path: str
) -> tuple[int, int]:
    """(Navidrome's entries, catalog entries) of ``path``: Navidrome's own entries in our
    answer are its own answer's, in its order, followed by the catalog's."""
    mine = entries(ours, path)
    own = [e for e in mine if not (e.get("id") or "").startswith("sh.")]
    added = mine[len(own) :]
    assert all((e.get("id") or "").startswith("sh.") for e in added)
    assert [canonical(e) for e in own] == [canonical(e) for e in entries(theirs, path)]
    return len(own), len(added)


ROLES = re.compile(rb"(?:<roles>[^<]*</roles>)+")


def roles_sorted(body: bytes) -> bytes:
    """The answer with each run of roles sorted (Navidrome's order of them changes)."""
    return ROLES.sub(lambda m: b"".join(sorted(re.findall(rb"<roles>[^<]*</roles>", m[0]))), body)


def without_catalog_entries(body: bytes, tags: str) -> bytes:
    """The answer's bytes without the catalog's entries (elements of ``tags`` with a
    catalog ID; they hold no element of the same name)."""
    entry = re.compile(rb"<(" + tags.encode() + rb') id="sh\.[^"]*".*?</\1>', re.DOTALL)
    return entry.sub(b"", body)


def catalog(entries_: list[ET.Element]) -> list[ET.Element]:
    return [e for e in entries_ if (e.get("id") or "").startswith("sh.")]


# --- Navidrome's own answers: the checks are right --------------------------------------------


def own_ids(world: DeliveryWorld) -> dict[str, str]:
    found = world.nd.client().ok("search3", {"query": "Hollow 2"})["searchResult3"]
    artist = world.nd.client().ok("search3", {"query": TERM})["searchResult3"]["artist"][0]
    return {"album": found["album"][0]["id"], "artist": artist["id"],
            "song": found["song"][0]["id"]}  # fmt: skip


OWN_CALLS = [
    ("search3", lambda i: {"query": TERM}),
    ("getAlbum", lambda i: {"id": i["album"]}),
    ("getArtist", lambda i: {"id": i["artist"]}),
    ("getSong", lambda i: {"id": i["song"]}),
    ("getMusicDirectory", lambda i: {"id": i["album"]}),
    ("getMusicDirectory", lambda i: {"id": i["artist"]}),
    ("getAlbumList2", lambda i: {"type": "newest"}),
    ("getStarred2", lambda i: {}),
    ("getArtistInfo2", lambda i: {"id": i["artist"]}),
    ("getAlbumInfo", lambda i: {"id": i["album"]}),
    ("getTopSongs", lambda i: {"artist": ARTIST}),
]


def test_navidrome_s_own_answers_pass_the_same_checks(world: DeliveryWorld) -> None:
    direct = navidrome(world)
    ids = own_ids(world)
    for method, params in OWN_CALLS:
        both(direct, method, params(ids))


def test_navidrome_s_json_written_as_xml_is_its_xml(world: DeliveryWorld) -> None:
    """Shijhon's XML writer, given Navidrome's JSON answer, writes Navidrome's own XML
    bytes, for every type (the writer of the catalog's entries)."""
    direct = navidrome(world)
    ids = own_ids(world)
    for method, params in OWN_CALLS:
        response = direct.request(method, params(ids), fmt="json").json()["subsonic-response"]
        envelope = ("status", "version", "type", "serverVersion", "openSubsonic")
        fields = {k: response.pop(k) for k in envelope if k in response}
        written = xmlform.answer(fields, response)
        own = direct.request(method, params(ids), fmt="xml").content
        assert roles_sorted(written) == roles_sorted(own), method


def test_navidrome_s_answers_read_and_written_back_are_the_same_bytes(
    world: DeliveryWorld,
) -> None:
    """The basis of every answer Shijhon extends: Navidrome's own elements stay as it wrote
    them (attributes, their order, escaping, children)."""
    direct = navidrome(world)
    ids = own_ids(world)
    for method, params in OWN_CALLS:
        body = direct.request(method, params(ids), fmt="xml").content
        assert xmlform.dumps(xmlform.load(body)) == body, method


# --- Shijhon's answers ------------------------------------------------------------------------


def test_search_results(world: DeliveryWorld, client: SubsonicClient) -> None:
    params = {"query": TERM}
    _, ours = both(client, "search3", params)
    assert ours.headers["content-encoding"] == "gzip"  # as Navidrome compresses it
    theirs = navidrome(world).request("search3", params, fmt="xml")
    # Navidrome's own entries are its bytes as it wrote them.
    own = without_catalog_entries(ours.content, "artist|album|song")
    assert roles_sorted(own) == roles_sorted(theirs.content)
    counts = {
        kind: own_elements_as_navidrome_wrote_them(ours, theirs, f"searchResult3/{kind}")
        for kind in ("artist", "album", "song")
    }
    assert counts["album"][0] and counts["album"][1]  # owned and catalog albums
    assert counts["song"][0] and counts["song"][1]
    _, ours = both(client, "search3", {"query": "wren loring"})
    assert catalog(entries(ours, "searchResult3/artist"))  # catalog artists too
    assert catalog(entries(ours, "searchResult3/song"))


def test_catalog_albums_songs_and_artists(client: SubsonicClient) -> None:
    album_id = f"sh.al.demo.{item('album-feat-standard')}"
    artist_id = f"sh.ar.demo.{item('artist-band')}"
    album, _ = both(client, "getAlbum", {"id": album_id})
    assert album["album"]["song"]
    song = album["album"]["song"][0]["id"]
    both(client, "getSong", {"id": song})
    both(client, "getSong", {"id": f"sh.tr.demo.{item('song-duo-feat')}"})
    artist, _ = both(client, "getArtist", {"id": artist_id})
    assert artist["artist"]["album"]
    for method in ("getArtistInfo", "getArtistInfo2"):
        info, _ = both(client, method, {"id": artist_id})
        assert info[method[3].lower() + method[4:]]["largeImageUrl"]
    for method in ("getAlbumInfo", "getAlbumInfo2"):
        info, _ = both(client, method, {"id": album_id})
        assert info["albumInfo"]["smallImageUrl"]
    for ident in (album_id, artist_id):  # catalog items as directories
        directory, _ = both(client, "getMusicDirectory", {"id": ident})
        assert directory["directory"]["child"]


def test_library_artist_pages(world: DeliveryWorld, client: SubsonicClient) -> None:
    ids = own_ids(world)
    _, ours = both(client, "getArtist", {"id": ids["artist"]})
    theirs = navidrome(world).request("getArtist", {"id": ids["artist"]}, fmt="xml")
    owned, added = own_elements_as_navidrome_wrote_them(ours, theirs, "artist/album")
    assert owned and added
    own = without_catalog_entries(ours.content, "album")
    assert roles_sorted(own) == roles_sorted(theirs.content)
    # The catalog's ID of an artist the library has: the library artist's page.
    page, _ = both(client, "getArtist", {"id": f"sh.ar.demo.{item('artist-duo')}"})
    assert page["artist"]["id"] == ids["artist"]


def test_errors(client: SubsonicClient, replay: Replay) -> None:
    album_id = f"sh.al.demo.{item('album-soundtrack-compilation')}"
    replay.failing[f"albums/{item('album-soundtrack-compilation')}"] = 503
    try:
        answer, _ = both(client, "getAlbum", {"id": album_id})
    finally:
        replay.failing.clear()
    assert answer["status"] == "failed" and answer["error"]["code"] == 0
    answer, _ = both(client, "getAlbum", {"id": "sh.al.demo.1"})  # not in the catalog
    assert answer["error"]["code"] == 70
    # Shijhon's own "ok" for an action on an item not in the library (nothing to change).
    answer, _ = both(client, "unstar", {"id": f"sh.tr.demo.{item('song-duo-feat')}"})
    assert answer["status"] == "ok"
