"""Suite A — fidelity.

Native responses through Shijhon are identical to direct Navidrome responses in status,
headers and body, allowing only documented differences:

- ``Date`` (two requests, two clocks);
- hop-by-hop headers (``Connection``, ``Keep-Alive``, ``Transfer-Encoding``), which belong
  to each connection;
- Navidrome's nondeterministic order of artist ``roles`` (JSON and XML).

Repeated parameters survive GET and form POST; ranges and HEAD are correct; a client
that disconnects mid-stream causes no error.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

import httpx
import pytest

from shijhon.proxy.params import body_kind, is_form, unreadable
from tests.conftest import NavidromeFactory
from tests.harness.library import Album, Track, simple_album, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance
from tests.harness.running import RunningServer
from tests.harness.subsonic import SubsonicClient

IGNORED_HEADERS = {"date", "connection", "keep-alive", "transfer-encoding"}
# Navidrome builds absolute URLs (artist images) from the request's Host, which the proxy
# passes through; comparisons therefore send the same public host both ways.
PUBLIC_HOST = {"host": "music.test"}


@dataclass
class World:
    navidrome: NavidromeInstance
    proxy: RunningServer
    songs: list[str]  # FLAC songs of the first album
    mp3_song: str
    m4a_song: str
    long_song: str
    album: str
    artist: str
    playlist: str

    def clients(self, fmt: str = "json") -> tuple[SubsonicClient, SubsonicClient]:
        direct = self.navidrome.client(fmt=fmt, salt="fixedsalt", headers=PUBLIC_HOST)  # type: ignore[arg-type]
        return direct, direct.with_base(self.proxy.base_url)


@pytest.fixture(scope="module")
def world(navidrome_factory: NavidromeFactory, shijhon_factory: Any) -> Iterator[World]:
    nd = navidrome_factory()
    flac = simple_album("Fidelity Artist", "First Light", 4, cover_file=True, genre="Rock")
    mp3 = simple_album("Fidelity Artist", "Second Wind", 2, fmt="mp3", embedded_cover=False)
    m4a = simple_album("Other Artist", "Third Rail", 2, fmt="m4a")
    long = Album("Fidelity Artist", "Long Player", (Track("Long One", 1, seconds=90),))
    for album in (flac, mp3, m4a, long):
        write_album(nd.music, album)
    nd.scan(full=True)
    nd.create_user("listener", "listener-password")
    admin = nd.client()

    def album_songs(title: str) -> list[dict[str, Any]]:
        hits = admin.ok("search3", {"query": title, "albumCount": 5, "songCount": 0})
        album = next(a for a in hits["searchResult3"]["album"] if a["name"] == title)
        songs: list[dict[str, Any]] = admin.ok("getAlbum", {"id": album["id"]})["album"]["song"]
        return songs

    first = album_songs("First Light")
    songs = [s["id"] for s in first]
    playlist = admin.ok("createPlaylist", {"name": "Fixture list", "songId": songs[:2]})
    admin.ok("star", {"id": songs[0]})
    admin.ok("setRating", {"id": songs[1], "rating": 4})
    admin.ok("createBookmark", {"id": songs[2], "position": 1500})
    world = World(
        navidrome=nd,
        proxy=shijhon_factory(nd),
        songs=songs,
        mp3_song=album_songs("Second Wind")[0]["id"],
        m4a_song=album_songs("Third Rail")[0]["id"],
        long_song=album_songs("Long Player")[0]["id"],
        album=first[0]["albumId"],
        artist=first[0]["artistId"],
        playlist=playlist["playlist"]["id"],
    )
    yield world


def header_items(response: httpx.Response) -> list[tuple[str, str]]:
    return sorted(
        (k.lower(), v)
        for k, v in response.headers.multi_items()
        if k.lower() not in IGNORED_HEADERS
    )


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        out = {k: _canonical(v) for k, v in value.items()}
        if isinstance(out.get("roles"), list):
            out["roles"] = sorted(out["roles"])
        return out
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def _canonical_xml(element: ElementTree.Element) -> Any:
    children = [_canonical_xml(child) for child in element]
    roles = sorted(c for c in children if c[0].endswith("}roles"))
    others = [c for c in children if not c[0].endswith("}roles")]
    return (element.tag, sorted(element.attrib.items()), element.text, others, roles)


def canonical_body(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return _canonical(json.loads(response.content))
    assert content_type.startswith(("application/xml", "text/xml")), content_type
    return _canonical_xml(ElementTree.fromstring(response.content))  # noqa: S314 - test data


def assert_same(direct: httpx.Response, proxied: httpx.Response) -> None:
    assert proxied.status_code == direct.status_code
    if proxied.content == direct.content:
        assert header_items(proxied) == header_items(direct)
        return
    # The only documented body nondeterminism is the order of artist roles (JSON and XML).
    # It also changes the (compressed) length, so Content-Length is checked for consistency.
    assert canonical_body(direct) == canonical_body(proxied)
    without_length = [h for h in header_items(direct) if h[0] != "content-length"]
    assert [h for h in header_items(proxied) if h[0] != "content-length"] == without_length
    for response in (direct, proxied):
        if "content-length" in response.headers:
            assert int(response.headers["content-length"]) == response.num_bytes_downloaded


def read_calls(w: World) -> list[tuple[str, dict[str, Any]]]:
    s = w.songs
    return [
        ("ping", {}),
        ("getLicense", {}),
        ("getMusicFolders", {}),
        ("getIndexes", {}),
        ("getArtists", {}),
        ("getArtist", {"id": w.artist}),
        ("getAlbum", {"id": w.album}),
        ("getSong", {"id": s[0]}),
        ("getMusicDirectory", {"id": w.album}),
        ("getAlbumList2", {"type": "alphabeticalByName", "size": 10}),
        ("getAlbumList2", {"type": "newest", "size": 10}),
        ("getAlbumList2", {"type": "starred"}),
        ("getAlbumList", {"type": "alphabeticalByArtist"}),
        ("getGenres", {}),
        ("getSongsByGenre", {"genre": "Rock"}),
        ("getStarred", {}),
        ("getStarred2", {}),
        ("getPlaylists", {}),
        ("getPlaylist", {"id": w.playlist}),
        ("getPlayQueue", {}),
        ("getBookmarks", {}),
        ("search2", {"query": "First"}),
        ("search3", {"query": "First", "songCount": 3, "songOffset": 1}),
        ("search3", {"query": "", "songCount": 100}),
        ("search3", {"query": '""'}),
        ("getUser", {"username": ADMIN_USER}),
        ("getScanStatus", {}),
        ("getArtistInfo2", {"id": w.artist}),
        ("getAlbumInfo2", {"id": w.album}),
        ("getTopSongs", {"artist": "Fidelity Artist"}),
        ("getLyricsBySongId", {"id": s[0]}),
        ("getInternetRadioStations", {}),
        ("getNowPlaying", {}),
        ("getOpenSubsonicExtensions", {}),
        ("getSong", {"id": "does-not-exist"}),
        ("notAMethod", {}),
    ]


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_read_endpoints_identical(world: World, fmt: str) -> None:
    direct, proxied = world.clients(fmt)
    for method, params in read_calls(world):
        # Navidrome's own answer can change between the two requests while it is still
        # settling the library (an album's cover art arrives with its artwork scan): a pair
        # is asked again only when Navidrome's answer itself changed meanwhile.
        for attempt in range(3):
            first = direct.request(method, params)
            try:
                assert_same(first, proxied.request(method, params))
                break
            except AssertionError as exc:
                settled = canonical_body(direct.request(method, params)) == canonical_body(first)
                if settled or attempt == 2:
                    raise AssertionError(f"{method} {params}: {exc}") from exc
                time.sleep(1)


# Random by design: compared on status, headers other than length, and response shape.
RANDOM_CALLS = [
    ("getRandomSongs", {"size": 3}),
    ("getSimilarSongs2", {"id": "{artist}"}),
    ("getAlbumList2", {"type": "random"}),
]


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_random_endpoints_same_shape(world: World, fmt: str) -> None:
    direct, proxied = world.clients(fmt)
    for method, params in RANDOM_CALLS:
        params = {k: str(v).format(artist=world.artist) for k, v in params.items()}
        d, p = direct.request(method, params), proxied.request(method, params)
        assert p.status_code == d.status_code == 200
        without_length = [h for h in header_items(d) if h[0] != "content-length"]
        assert [h for h in header_items(p) if h[0] != "content-length"] == without_length
        if fmt == "json":
            assert d.json()["subsonic-response"].keys() == p.json()["subsonic-response"].keys()


def test_view_suffix_post_and_gzip(world: World) -> None:
    direct, proxied = world.clients()
    for kwargs in (
        {"view_suffix": True},
        {"http_method": "POST"},
        {"headers": {"accept-encoding": "gzip"}},
    ):
        params = {"type": "alphabeticalByName"}
        assert_same(
            direct.request("getAlbumList2", params, **kwargs),
            proxied.request("getAlbumList2", params, **kwargs),
        )


@pytest.mark.parametrize("size", [None, "64"])
def test_cover_art_identical(world: World, size: str | None) -> None:
    direct, proxied = world.clients()
    params = {"id": f"al-{world.album}", **({"size": size} if size else {})}
    first = direct.request("getCoverArt", params)
    assert first.status_code == 200
    assert_same(direct.request("getCoverArt", params), proxied.request("getCoverArt", params))


@pytest.mark.parametrize("song_attr", ["songs", "mp3_song", "m4a_song"])
@pytest.mark.parametrize(
    "range_header", [None, "bytes=0-99", "bytes=100-", "bytes=-64", "bytes=0-", "bytes=999999-"]
)
def test_stream_and_ranges_identical(
    world: World, song_attr: str, range_header: str | None
) -> None:
    value = getattr(world, song_attr)
    song = value[0] if isinstance(value, list) else value
    direct, proxied = world.clients()
    headers = {"range": range_header} if range_header else {}
    for method in ("stream", "download"):
        d = direct.request(method, {"id": song}, headers=headers)
        p = proxied.request(method, {"id": song}, headers=headers)
        assert_same(d, p)
        if range_header is None:
            assert p.headers["content-length"] == str(len(p.content))


def test_head_identical(world: World) -> None:
    direct, proxied = world.clients()
    for headers in ({}, {"range": "bytes=10-20"}):
        d = direct.request("stream", {"id": world.songs[0]}, http_method="HEAD", headers=headers)
        p = proxied.request("stream", {"id": world.songs[0]}, http_method="HEAD", headers=headers)
        assert_same(d, p)
        assert p.headers.get("content-length")


def test_transcoding_through_proxy(world: World) -> None:
    _, proxied = world.clients()
    response = proxied.request("stream", {"id": world.songs[0], "maxBitRate": 96, "format": "mp3"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert len(response.content) > 1000


def test_repeated_parameters_get_and_form_post(world: World) -> None:
    direct, proxied = world.clients()
    a, b, c, d = world.songs
    # star with repeated id: GET, then unstar with repeated id: form POST
    proxied.ok("star", {"id": [c, d]})
    starred = {s["id"] for s in direct.ok("getStarred2")["starred2"]["song"]}
    assert {c, d} <= starred
    proxied.ok("unstar", {"id": [c, d]}, http_method="POST")
    starred = {s["id"] for s in direct.ok("getStarred2")["starred2"].get("song", [])}
    assert not {c, d} & starred

    created = proxied.ok(
        "createPlaylist", {"name": "Repeats", "songId": [a, b, a, c]}, http_method="POST"
    )["playlist"]
    entries = direct.ok("getPlaylist", {"id": created["id"]})["playlist"]["entry"]
    assert [e["id"] for e in entries] == [a, b, a, c]
    proxied.ok(
        "updatePlaylist",
        {"playlistId": created["id"], "songIdToAdd": [d, d], "songIndexToRemove": [0, 2]},
    )
    entries = direct.ok("getPlaylist", {"id": created["id"]})["playlist"]["entry"]
    assert [e["id"] for e in entries] == [b, c, d, d]

    proxied.ok(
        "savePlayQueue", {"id": [a, b, a], "current": b, "position": 1234}, http_method="POST"
    )
    queue = direct.ok("getPlayQueue")["playQueue"]
    assert [e["id"] for e in queue["entry"]] == [a, b, a]
    assert queue["current"] == b and queue["position"] == 1234

    now = int(time.time() * 1000)
    proxied.ok("scrobble", {"id": [c, d], "time": [now, now + 1], "submission": "true"})
    for song in (c, d):
        assert direct.ok("getSong", {"id": song})["song"]["playCount"] >= 1


def test_a_large_form_is_read_as_navidrome_reads_it(world: World) -> None:
    _, proxied = world.clients()
    pad = "x" * (1024 * 1024 + 10)
    body = proxied.ok("ping", {"pad": pad}, http_method="POST")
    assert body["status"] == "ok"
    # Its parameters count, as for Navidrome: the format, after 1 MiB of the form.
    xml = proxied.http.post(
        f"{proxied.base_url}/rest/ping",
        content=urlencode([("pad", pad), *proxied.auth_params()]).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert xml.headers["content-type"].startswith("application/xml") and 'status="ok"' in xml.text


def test_as_many_parameters_as_navidrome_reads(world: World) -> None:
    """Navidrome 0.64.2 reads 10,000 parameters of a form (Go's limit) and answers one
    with more with an error: the same through Shijhon, which reads no more either."""
    direct, proxied = world.clients()
    form = {"content-type": "application/x-www-form-urlencoded"}
    credentials = urlencode([*proxied.auth_params(), ("f", "json")])
    pairs = len(credentials.split("&"))
    for count, status in ((10_000, "ok"), (10_001, "failed")):
        body = (
            "&".join(["id=0123456789abcdefghijkl"] * (count - pairs)) + "&" + credentials
        ).encode()
        answers = [
            httpx.post(f"{client.base_url}/rest/ping", content=body, headers=form)
            for client in (direct, proxied)
        ]
        assert answers[0].status_code == answers[1].status_code == 200
        assert answers[0].content == answers[1].content
        assert f'status="{status}"' in answers[1].text or f'"status":"{status}"' in answers[1].text
    assert "parameters exceeded limit" in answers[1].text


@pytest.mark.parametrize(
    "content_type",
    [
        b"application/x-www-form-urlencoded",
        b"Application/X-WWW-Form-Urlencoded",
        b"APPLICATION/X-WWW-FORM-URLENCODED;CHARSET=UTF-8",
        b"application/x-www-form-urlencoded  ; charset=utf-8",
        b"application/x-www-form-urlencoded;",
        b"application/x-www-form-urlencoded\xc2\xa0",
        b"application/x-www-form-urlencoded\xa0",
        "appl\u0130cation/x-www-form-urlencoded".encode(),
        b"application/x-www-form-urlencoded-not",
        b"application /x-www-form-urlencoded",
        b"multipart/form-data; boundary=x",
        b"application/json",
        b"text/plain",
        # Media types Go's parser refuses: Navidrome answers such a request with an error
        # (Shijhon reads the form, and Navidrome's answer goes out).
        b"application/x-www-form-urlencoded; a=1; a=2",
        b"application/x-www-form-urlencoded; a=1; a=1",
        b"application/x-www-form-urlencoded; charset",
        b"application/x-www-form-urlencoded; charset=",
        b'application/x-www-form-urlencoded; charset="utf-8',
        b'application/x-www-form-urlencoded; charset="utf-8"; x="a;b"',
        b"application/x-www-form-urlencoded; charset=utf-8;",
        b"application/x-www-form-urlencoded; charset=utf-8; ;",
        b"application/x-www-form-urlencoded charset=utf-8",
        b'application/x-www-form-urlencoded; x="a\\ b"; x="a b"',
        b'application/x-www-form-urlencoded; x="a\\ b"; x="a\\\\ b"',
        b'application/x-www-form-urlencoded; x="a\\"b"; x="a"b"',
        "application/x-www-form-urlencoded;\u2003charset=utf-8".encode(),
        "application/x-www-form-urlencoded\u2003; charset=utf-8".encode(),
        b'application/x-www-form-urlencoded; x="\xff"; x="\xfe"',
        b'application/x-www-form-urlencoded; x="\xff"; x="\xff"',
        b"text/plain; charset",
        b"not a media type",
        b"text/",
        b"/plain",
        b"text",
    ],
)
def test_a_form_is_what_navidrome_reads_as_one(world: World, content_type: bytes) -> None:
    """What Shijhon takes for a form (its parameters count) is what Navidrome reads as one:
    here its credentials, sent in the body alone. Never the other way round: a form for
    Navidrome that Shijhon does not read."""
    direct, proxied = world.clients()
    body = urlencode([*direct.auth_params(), ("f", "json")]).encode()
    headers = [(b"content-type", content_type)]
    answers = [
        httpx.post(f"{base}/rest/ping", content=body, headers=headers)
        for base in (direct.base_url, proxied.base_url)
    ]
    read = answers[0].headers.get("content-type", "").startswith("application/json") and (
        answers[0].json()["subsonic-response"]["status"] == "ok"
    )
    # What Navidrome refuses as a whole (its error names Go's parser), Shijhon forwards.
    refused = b"mime: " in answers[0].content
    assert (body_kind("POST", headers) == "refused") == refused, answers[0].text
    assert is_form("POST", headers) == read
    assert answers[0].status_code == answers[1].status_code
    assert answers[0].content == answers[1].content


@pytest.mark.parametrize(
    ("query", "form"),
    [
        ("pad=%zz", ""),  # a percent sign that begins no escape
        ("pad=100%", ""),
        ("pad=%4", ""),
        ("pad=one;two", ""),  # a semicolon between parameters
        ("", "pad=%zz"),
        ("", "pad=one;two"),
        ("pad=%41%2f%2F+x", "pad=%7e"),  # (ordinary escapes: read)
    ],
)
def test_parameters_navidrome_cannot_read_are_its_refusal(
    world: World, query: str, form: str
) -> None:
    """Navidrome answers a query or a form that Go's parser refuses - a broken escape, a
    semicolon - with an error, whatever else it holds: what Shijhon takes for unreadable
    is what Navidrome refuses, and its answer goes out."""
    direct, proxied = world.clients()
    credentials = urlencode([*direct.auth_params(), ("f", "json")])
    sent = {"content-type": "application/x-www-form-urlencoded"}
    answers = [
        httpx.post(f"{base}/rest/ping?{credentials}&{query}", content=form.encode(), headers=sent)
        for base in (direct.base_url, proxied.base_url)
    ]
    refused = answers[0].json()["subsonic-response"]["status"] == "failed"
    assert (unreadable(query.encode()) or unreadable(form.encode())) == refused
    assert answers[0].status_code == answers[1].status_code == 200
    assert answers[0].content == answers[1].content


def test_non_subsonic_paths_identical(world: World) -> None:
    nd, proxy = world.navidrome.base_url, world.proxy.base_url
    for path in ("/ping", "/app/", "/favicon.ico", "/nothing-here"):
        assert_same(httpx.get(nd + path), httpx.get(proxy + path))
    login = httpx.post(
        proxy + "/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    )
    assert login.status_code == 200
    headers = {"x-nd-authorization": f"Bearer {login.json()['token']}"}
    path = "/api/album?_start=0&_end=10&_sort=name&_order=ASC"
    # Earlier writes (scrobbles) update play statistics in the background; compare only
    # when two direct reads around the proxied one agree.
    for _ in range(20):
        d = httpx.get(nd + path, headers=headers)
        p = httpx.get(proxy + path, headers=headers)
        if httpx.get(nd + path, headers=headers).content == d.content:
            break
        time.sleep(0.2)
    # Navidrome refreshes the token on every native API call.
    ignored = {"x-nd-authorization"}
    assert p.status_code == d.status_code == 200
    assert [h for h in header_items(p) if h[0] not in ignored] == [
        h for h in header_items(d) if h[0] not in ignored
    ]
    assert p.content == d.content


def test_wrong_credentials_get_navidromes_error(world: World) -> None:
    bad_direct = SubsonicClient(
        world.navidrome.base_url, "listener", "wrong", salt="fixedsalt", headers=PUBLIC_HOST
    )
    bad_proxied = bad_direct.with_base(world.proxy.base_url)
    for method in ("ping", "getAlbum", "stream"):
        assert_same(
            bad_direct.request(method, {"id": world.album}),
            bad_proxied.request(method, {"id": world.album}),
        )


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_client_disconnects_silently(world: World) -> None:
    collector = _Collect()
    logging.getLogger().addHandler(collector)
    try:
        client = world.navidrome.client()
        query = str(httpx.QueryParams([*client.auth_params(), ("id", world.long_song)]))
        for extra in ("", "&format=mp3&maxBitRate=96"):
            for _ in range(3):
                with socket.create_connection(("127.0.0.1", world.proxy.port)) as sock:
                    request = f"GET /rest/stream?{query}{extra} HTTP/1.1\r\nHost: x\r\n\r\n"
                    sock.sendall(request.encode())
                    sock.recv(4096)
        time.sleep(0.5)
        _, proxied = world.clients()
        proxied.ok("ping")
    finally:
        logging.getLogger().removeHandler(collector)
    assert [r.getMessage() for r in collector.records] == []
