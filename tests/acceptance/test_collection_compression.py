"""Virtual album GET and native/selected HEAD framing through completion wrappers."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from defusedxml import ElementTree

from shijhon.catalog.model import CatalogRef
from shijhon.proxy.app import Reply
from shijhon.views.answers import MIN_COMPRESSED, LibraryAnswer
from shijhon.views.ids import album_id as catalog_album_id
from tests.acceptance.test_A_fidelity import canonical_body
from tests.acceptance.test_I_fill import OPENED
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser
from tests.harness.delivery import delivery_world
from tests.harness.library import simple_album, write_album
from tests.harness.replay import Replay
from tests.harness.subsonic import SubsonicClient

CALLBACK = "$fixture.completion"


@pytest.fixture(scope="module")
def cached_completion(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[SubsonicClient, dict[str, Any], str, SubsonicClient]]:
    nd = navidrome_factory()
    write_album(nd.music, OPENED)
    # One short recording with enough metadata to put even XML lists above the threshold.
    companion = "Framing Companion " + "Synthetic " * 10 + "Album"
    write_album(nd.music, simple_album(OPENED.artist, companion, 1))
    nd.scan(full=True)
    owner = nd.create_user("compression-listener", "fixture-password")
    native = nd.client("compression-listener", "fixture-password", salt="compression-salt")
    replay = Replay()
    replay.aliases["neve ashdown northern letters (remastered)"] = "tender rivers"
    try:
        album = native.ok("search3", {"query": OPENED.title, "artistCount": 0, "songCount": 0})[
            "searchResult3"
        ]["album"][0]["id"]
        original = native.ok("getAlbum", {"id": album})["album"]
        with delivery_world(
            nd,
            tmp_path_factory.mktemp("completion-framing"),
            catalog=replay.catalog(),
            fill={
                "enabled": True,
                "auto_min_songs": 50,
                "library_pass": "off",
                "open_budget_seconds": 15,
            },
        ) as world:
            browser = Browser(world.server.base_url)
            try:
                assert (
                    browser.sign_in("compression-listener", "fixture-password").status_code == 303
                )
            finally:
                browser.close()
            collection = world.services.collection
            assert collection is not None
            world.server.call(lambda: collection.refresh(owner, "1"))
            client = native.with_base(world.server.base_url)
            try:
                assert client.ok("getAlbum", {"id": album})["album"]["songCount"] == 17

                async def cached_plan() -> str:
                    assert world.services.fills is not None
                    shown = await world.services.fills.shown([album])
                    assert shown[album].owned == 2 and len(shown[album].missing) == 15
                    row = await world.services.store.fetchone(
                        "SELECT release_ref FROM album_matches WHERE album_id=?", [album]
                    )
                    assert row is not None
                    return catalog_album_id(CatalogRef.parse(row["release_ref"]))

                discovery = world.server.call(cached_plan)
                assert world.placeholder_rows() == 0
                yield client, original, discovery, native
            finally:
                client.close()
    finally:
        native.close()


@pytest.mark.parametrize("method", ["getAlbum", "getArtist", "getAlbumList2"])
@pytest.mark.parametrize("fmt", ["json", "xml", "jsonp"])
@pytest.mark.parametrize("coding", ["gzip", "identity"])
def test_cached_completion_head_keeps_native_or_selected_representation(
    cached_completion: tuple[SubsonicClient, dict[str, Any], str, SubsonicClient],
    method: str,
    fmt: str,
    coding: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, original, _, native = cached_completion
    # Explicit album and artist pages answer HEAD and JSONP with Navidrome's own page,
    # the library list with its selected representation. Their JSON/XML GET carries the
    # complete counts of the album shown complete, as the album page shows them; only
    # the album page adds the missing songs.
    page = method in ("getAlbum", "getArtist")
    completed = fmt in ("json", "xml")
    virtual = completed and method == "getAlbum"
    opened = client.ok("getAlbum", {"id": original["id"]})["album"]
    assert opened["songCount"] == len(opened["song"]) == 17
    assert opened["duration"] > original["duration"]
    reply = LibraryAnswer.reply
    head_bodies: list[bytes] = []

    def observed(answer: LibraryAnswer, **kwargs: Any) -> Reply:
        if kwargs.get("head"):
            assert answer.selected_view == (not page)
            assert kwargs.get("document") is None
            head_bodies.append(answer.body)
        return reply(answer, **kwargs)

    monkeypatch.setattr(LibraryAnswer, "reply", observed)
    params = {
        "getAlbum": {"id": original["id"]},
        "getArtist": {"id": original["artistId"]},
        "getAlbumList2": {"type": "alphabeticalByName"},
    }[method]
    if fmt == "jsonp":
        params = {**params, "f": "jsonp", "callback": CALLBACK}
    headers = {"Accept-Encoding": coding, "Origin": "https://client.example"}
    get = client.request(
        method, params, fmt="json" if fmt == "json" else "xml", headers=headers, stream=True
    )
    try:
        wire = b"".join(get.iter_raw())
    finally:
        get.close()
    head = client.request(
        method, params, fmt="json" if fmt == "json" else "xml", headers=headers, http_method="HEAD"
    )
    assert head.status_code == get.status_code == 200
    assert head.content == b""
    for field in (
        "content-type",
        "content-encoding",
        "vary",
        "cache-control",
        "access-control-allow-origin",
        "access-control-expose-headers",
    ):
        assert head.headers.get(field) == get.headers.get(field), field
    assert get.headers["content-length"] == str(len(wire))
    assert get.headers.get("content-encoding") == ("gzip" if coding == "gzip" else None)
    assert get.headers["vary"] == ("Accept-Encoding, Origin" if coding == "gzip" else "Origin")
    body = gzip.decompress(wire) if coding == "gzip" else wire
    assert len(body) >= MIN_COMPRESSED
    assert len(head_bodies) == 1
    head_body = head_bodies[0]
    head_wire = gzip.compress(head_body, 5) if coding == "gzip" else head_body
    assert head.headers["content-length"] == str(len(head_wire))

    def canonical(data: bytes) -> Any:
        if fmt == "jsonp":
            assert data.startswith(CALLBACK.encode() + b"(") and data.endswith(b")")
            data = data[len(CALLBACK) + 1 : -1]
        return canonical_body(
            httpx.Response(
                200,
                headers={"content-type": "application/xml" if fmt == "xml" else "application/json"},
                content=data,
            )
        )

    def uncounted(data: bytes) -> Any:
        """The artist page or list without the shown album's songCount and duration."""
        if fmt == "xml":
            root = ElementTree.fromstring(data)
            [card] = [e for e in root.iter() if e.get("id") == original["id"]]
            del card.attrib["songCount"], card.attrib["duration"]
            return canonical(ElementTree.tostring(root))
        document = json.loads(data)
        albums = document["subsonic-response"][key]["album"]
        [card] = [a for a in albums if a["id"] == original["id"]]
        del card["songCount"], card["duration"]
        return canonical(json.dumps(document).encode())

    key = {"getAlbum": "album", "getArtist": "artist", "getAlbumList2": "albumList2"}[method]
    if completed and not page:
        # The list's GET is its selected representation (HEAD's) but for the album's
        # counts: the same albums, order and X-Total-Count.
        assert uncounted(body) == uncounted(head_body)
        if fmt == "xml":
            listed = ElementTree.fromstring(head_body)
            [card] = [e for e in listed.iter() if e.get("id") == original["id"]]
            assert card.attrib["songCount"] == "2"
            assert int(card.attrib["duration"]) == original["duration"]
            # Byte for byte but for the two counts (attribute order and escaping).
            start = head_body.index(b'<album id="' + original["id"].encode() + b'"')
            end = head_body.index(b">", start)
            tag = head_body[start:end].replace(b' songCount="2"', b' songCount="17"')
            tag = tag.replace(
                f' duration="{original["duration"]}"'.encode(),
                f' duration="{opened["duration"]}"'.encode(),
            )
            assert body == head_body[:start] + tag + head_body[end:]
        else:
            cards = json.loads(head_body)["subsonic-response"][key]["album"]
            [card] = [a for a in cards if a["id"] == original["id"]]
            assert (card["songCount"], card["duration"]) == (2, original["duration"])
    elif completed:
        # Completion extends JSON/XML GET only. HEAD describes the native page, without
        # the album's unmaterialized tracks or complete counts. (Navidrome's artist
        # image URL names the requested host.)
        host = httpx.URL(client.base_url).netloc.decode()
        native_get = native.request(method, params, fmt=fmt, headers={**headers, "Host": host})  # type: ignore[arg-type]
        assert native_get.status_code == 200
        assert canonical(head_body) == canonical_body(native_get)
        assert head.headers["content-type"] == native_get.headers["content-type"].split(";", 1)[0]
        if method == "getArtist":
            # Only the album card's counts differ from Navidrome's own page.
            assert uncounted(body) == uncounted(native_get.content)
            if fmt == "json":
                cards = native_get.json()["subsonic-response"]["artist"]["album"]
                [card] = [a for a in cards if a["id"] == original["id"]]
                assert (card["songCount"], card["duration"]) == (2, original["duration"])
            else:
                root = ElementTree.fromstring(native_get.content)
                [card] = [e for e in root.iter() if e.get("id") == original["id"]]
                assert card.attrib["songCount"] == "2"
                assert int(card.attrib["duration"]) == original["duration"]
        elif fmt == "json":
            native_album = native_get.json()["subsonic-response"]["album"]
            assert native_album["songCount"] == len(native_album["song"]) == 2
            assert native_album["duration"] == original["duration"]
        else:
            native_album = ElementTree.fromstring(native_get.content).find(
                "{http://subsonic.org/restapi}album"
            )
            assert native_album is not None and native_album.attrib["songCount"] == "2"
            assert len(native_album.findall("{http://subsonic.org/restapi}song")) == 2
            assert int(native_album.attrib["duration"]) == original["duration"]
    elif head_body == body:
        assert head.headers["content-length"] == get.headers["content-length"]
    else:
        # Native artist role order can change between two real reads, affecting gzip
        # size. Check HEAD against its own unchanged representation, and require the
        # two documents to agree using the existing role-order-only canonicalization.
        assert method == "getArtist"

        assert canonical(head_body) == canonical(body)
    assert get.headers["cache-control"] == "no-store"
    # The answer's entries: the album page's songs (complete in its GET), or the albums
    # of the artist page and of the list. HEAD counts those of its own representation.
    assert get.headers["x-total-count"] == ("17" if virtual else "2")
    assert head.headers["x-total-count"] == "2"
    assert get.headers["access-control-allow-origin"] == "*"
    assert get.headers["access-control-expose-headers"] == (
        "X-Content-Duration, X-Total-Count, X-Nd-Authorization"
    )
    assert (
        get.headers["content-type"]
        == {
            "json": "application/json",
            "xml": "application/xml",
            "jsonp": "application/javascript",
        }[fmt]
    )
    if fmt == "xml":
        namespace = "{http://subsonic.org/restapi}"
        root = ElementTree.fromstring(body)
        assert root.attrib["status"] == "ok"
        container = root.find(namespace + key)
        assert container is not None
        albums = [container] if method == "getAlbum" else container.findall(namespace + "album")
        album = next(a for a in albums if a.attrib["id"] == original["id"])
        assert int(album.attrib["songCount"]) == (17 if completed else 2)
        if method == "getAlbum":
            songs = album.findall(namespace + "song")
            assert len(songs) == (17 if virtual else 2)
            assert [s.attrib["id"] for s in songs if not s.attrib["id"].startswith("sh.")] == [
                s["id"] for s in original["song"]
            ]
            missing = [s for s in songs if s.attrib["id"].startswith("sh.tr.")]
            assert len(missing) == (15 if virtual else 0)
            assert int(album.attrib["duration"]) == original["duration"] + sum(
                int(s.attrib["duration"]) for s in missing
            )
        else:
            assert len(albums) == int(get.headers["x-total-count"])
            assert album.findall(namespace + "song") == []
            assert int(album.attrib["duration"]) == (
                opened["duration"] if completed else original["duration"]
            )
    else:
        if fmt == "jsonp":
            assert body.startswith(CALLBACK.encode() + b"(") and body.endswith(b")")
            body = body[len(CALLBACK) + 1 : -1]
        document = json.loads(body)["subsonic-response"]
        assert document["status"] == "ok"
        albums = [document[key]] if method == "getAlbum" else document[key]["album"]
        album = next(a for a in albums if a["id"] == original["id"])
        assert album["songCount"] == (17 if completed else 2)
        if method == "getAlbum":
            assert len(album["song"]) == (17 if virtual else 2)
            assert [s["id"] for s in album["song"] if not s["id"].startswith("sh.")] == [
                s["id"] for s in original["song"]
            ]
            missing = [s for s in album["song"] if s["id"].startswith("sh.tr.")]
            assert len(missing) == (15 if virtual else 0)
            assert album["duration"] == original["duration"] + sum(s["duration"] for s in missing)
        else:
            assert len(albums) == int(get.headers["x-total-count"])
            assert "song" not in album
            assert album["duration"] == (opened["duration"] if completed else original["duration"])


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_cached_completion_catalog_discovery_retains_gzip(
    cached_completion: tuple[SubsonicClient, dict[str, Any], str, SubsonicClient], fmt: str
) -> None:
    client, _, discovery, _ = cached_completion
    answer = client.request(
        "getAlbum", {"id": discovery}, fmt=fmt, headers={"Accept-Encoding": "gzip"}
    )
    assert answer.status_code == 200
    assert len(answer.content) >= MIN_COMPRESSED
    assert answer.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in answer.headers["vary"]
    if fmt == "json":
        album = answer.json()["subsonic-response"]["album"]
        assert album["songCount"] == len(album["song"]) == 17
    else:
        album = ElementTree.fromstring(answer.content).find("{http://subsonic.org/restapi}album")
        assert album is not None and album.attrib["songCount"] == "17"
        assert len(album.findall("{http://subsonic.org/restapi}song")) == 17
