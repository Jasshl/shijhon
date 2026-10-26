"""Ordered imports against disposable Navidrome with invented library/catalog music."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mutagen.flac import FLAC

from shijhon.catalog.base import SearchResults
from shijhon.playlists.model import Connection, PlaylistError
from tests.acceptance.test_I_exact_release import FILLERS, filler
from tests.acceptance.test_R_playlists import RELEASE, SONGS, connected_admin, wait_state
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[DeliveryWorld, Browser]]:
    nd = navidrome_factory()
    for album in (
        Album(
            "Glass Orchard", "Paper Garden", (Track("First Light", 1), Track("Harbor (Live)", 2))
        ),
        Album("Tideline", "First Room", (Track("Same Song", 1),)),
        Album("Tideline", "Second Room", (Track("Same Song", 1),)),
    ):
        write_album(nd.music, album)
    for path in write_album(
        nd.music,
        Album(
            "Invented untagged",
            "Folder only",
            (Track("Useful Filename", 1),),
            folder="Organized originals/Folder only",
        ),
    ):
        FLAC(path).delete()
    nd.scan()
    catalog = Replay().catalog()

    async def search(term: str, limit: int = 20) -> SearchResults:
        return SearchResults(songs=tuple(s for s in SONGS if s.title in term))

    catalog.search = search  # type: ignore[method-assign]
    with delivery_world(nd, tmp_path_factory.mktemp("imports"), catalog=catalog) as w:
        browser = Browser(w.server.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        assert (
            browser.submit(
                "playlists", {"password": ADMIN_PASSWORD, "backend": "none"}, "playlists/connection"
            ).status_code
            == 303
        )
        yield w, browser
        browser.close()


def submit(
    world: tuple[DeliveryWorld, Browser], *, lines: str = "", csv: str = ""
) -> tuple[str, str]:
    w, browser = world
    fields = browser.form("playlists/import")
    fields.update({"name": "Invented ordered import", "lines": lines})
    if csv:
        fields.pop("csv", None)
        answer = browser.http.post(
            "/shijhon/playlists/import",
            data=fields,
            files={"csv": ("invented.csv", csv.encode(), "text/csv")},
        )
    else:
        answer = browser.post("playlists/import", fields)
    assert answer.status_code == 303, answer.text
    ident = answer.headers["location"].rsplit("/", 1)[1]
    service = w.services.playlists
    assert service
    run = w.server.call(lambda: service.repo.runs(ident))[0]
    result = wait_state(w, run["id"], ("preview", "failed", "uncertain", "published"))
    assert result["state"] == "preview", result
    return ident, run["id"]


def test_csv_import_over_curation_limit_keeps_repeats_and_native_ids(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    before = w.placeholder_rows()
    ident, run = submit(
        world,
        csv="artist,title,album,version\n" + "Glass Orchard,First Light,Paper Garden,\n" * 101,
    )
    result = w.server.call(lambda: service.repo.run(run))
    assert len(result["data"]["selected"]) == 101
    assert len({s["id"] for s in result["data"]["selected"]}) == 1
    assert w.placeholder_rows() == before
    page = browser.get(f"playlists/import/{ident}")
    assert "101 entries · 101 found" in page.text
    assert (
        browser.post(f"playlists/import/{ident}", {"action": "publish", "run": run}).status_code
        == 403
    )
    response = browser.submit(f"playlists/import/{ident}", {"action": "publish"})
    assert response.status_code == 303, response.text
    result = wait_state(w, run, ("published", "failed", "uncertain"))
    assert result["state"] == "published", result["error"]

    async def verify() -> None:
        connection = await connected_admin(service)
        mix = await service.repo.mix(ident, connection.user_id)
        saved = await service.client.playlist(connection, mix["playlist_id"])
        assert [s["id"] for s in saved["entry"]] == result["data"]["native_ids"]
        assert len(saved["entry"]) == 101
        assert result["data"]["native_ids"] == [result["data"]["selected"][0]["id"]] * 101
        assert result["data"]["import_review"] == {"total": 101, "excluded": []}

    w.server.call(verify)


def test_partial_import_lists_every_row_and_requires_current_explicit_exclusions(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    ident, run = submit(
        world,
        lines=(
            "Glass Orchard — Harbor (Live)\nTideline — Same Song\n"
            "Absent Artist — Missing Song\nGlass Orchard — First Light\n"
            "Glass Orchard — Harbor (Live)"
        ),
    )
    preview = w.server.call(lambda: service.repo.run(run))
    assert [r["state"] for r in preview["data"]["rows"]] == [
        "matched",
        "ambiguous",
        "unresolved",
        "matched",
        "matched",
    ]
    page = browser.get(f"playlists/import/{ident}")
    assert (
        "Publish without the entries not found (2)" in page.text and "Several matches" in page.text
    )
    old_form = browser.form(f"playlists/import/{ident}")
    response = browser.submit(f"playlists/import/{ident}", {"action": "publish"})
    assert response.status_code == 200 and "confirm the excluded entry count" in response.text
    response = browser.submit(f"playlists/import/{ident}", {"action": "publish", "excluded": "1"})
    assert response.status_code == 200
    assert w.server.call(lambda: service.repo.run(run))["state"] == "preview"
    response = browser.submit(
        f"playlists/import/{ident}", {"action": "publish", "excluded": "2", "review": "stale"}
    )
    assert response.status_code == 200 and "review changed" in response.text
    # The unrelated described-playlist route cannot bypass import review.
    answer = browser.post(
        f"playlists/describe/{ident}", {"csrf": old_form["csrf"], "run": run, "action": "publish"}
    )
    assert answer.status_code == 200
    assert w.server.call(lambda: service.repo.run(run))["state"] == "preview"
    response = browser.submit(f"playlists/import/{ident}", {"action": "publish", "excluded": "2"})
    assert response.status_code == 303
    result = wait_state(w, run, ("published", "failed", "uncertain"))
    assert result["state"] == "published", result["error"]
    assert result["data"]["import_review"] == {"total": 5, "excluded": [2, 3]}
    assert [s["title"] for s in result["data"]["selected"]] == [
        "Harbor (Live)",
        "First Light",
        "Harbor (Live)",
    ]
    assert result["data"]["native_ids"][0] == result["data"]["native_ids"][2]
    assert "5 entries · 3 found · 2 not found" in browser.get(f"playlists/import/{ident}").text


def test_catalog_import_adds_only_after_review_and_recovers_lost_create_response(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    before = w.placeholder_rows()
    ident, run = submit(
        world,
        csv="artist,title,album,version\n"
        + "\n".join(
            f"{s.artist},{s.title},{RELEASE.title}," for s in (SONGS[1], SONGS[0], SONGS[1])
        ),
    )
    assert w.placeholder_rows() == before
    original_call = service.client.call

    async def lost_reply(connection: Connection, method: str, params: Any = ()) -> dict[str, Any]:
        answer = await original_call(connection, method, params)
        if method == "createPlaylist":
            raise PlaylistError("The synthetic response was interrupted.")
        return answer

    async def install() -> None:
        service.client.call = lost_reply

    w.server.call(install)
    try:
        assert browser.submit(f"playlists/import/{ident}", {"action": "publish"}).status_code == 303
        result = wait_state(w, run, ("published", "failed", "uncertain"))
        assert result["state"] == "uncertain", result
        assert result["data"]["intent"] == "create" and not result["data"]["created_id"]
    finally:

        async def restore() -> None:
            service.client.call = original_call

        w.server.call(restore)
    frozen = result["data"]
    assert browser.submit(f"playlists/import/{ident}", {"action": "retry"}).status_code == 303
    result = wait_state(w, run, ("published", "failed", "uncertain"))
    assert result["state"] == "published", result["error"]
    assert result["data"]["selected"] == frozen["selected"]
    assert result["data"]["native_ids"][0] == result["data"]["native_ids"][2]
    assert w.placeholder_rows() >= before + len(RELEASE.tracks)

    async def verify() -> None:
        connection = await connected_admin(service)
        mix = await service.repo.mix(ident, connection.user_id)
        saved = await service.client.playlist(connection, mix["playlist_id"])
        assert [s["id"] for s in saved["entry"]] == result["data"]["native_ids"]
        assert [s["title"] for s in saved["entry"]] == [
            SONGS[1].title,
            SONGS[0].title,
            SONGS[1].title,
        ]
        listing = await service.client.call(connection, "getPlaylists")
        assert sum(p["id"] == saved["id"] for p in listing["playlists"]["playlist"]) == 1
        assert not any(p["name"] == frozen["provisional"] for p in listing["playlists"]["playlist"])

    w.server.call(verify)


def test_large_native_m3u_paths_include_untagged_files_and_reconcile_lost_append(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    songs = w.nd.client().ok("search3", {"query": "", "songCount": 500})["searchResult3"]["song"]
    tagged = next(
        s for s in songs if s["title"] == "First Light" and s["artist"] == "Glass Orchard"
    )
    untagged = next(s for s in songs if "Useful Filename" in s.get("path", ""))
    assert untagged["artist"] != "Invented untagged"
    physical = {
        s["id"]: s["path"]
        for s in w.nd.native(
            "GET",
            "song",
            params={
                "library_id": 1,
                "_start": 0,
                "_end": 500,
                "missing": "false",
            },
        ).json()
    }
    source = "#EXTM3U\n" + f"{physical[tagged['id']]}\n{physical[untagged['id']]}\n" * 501
    fields = browser.form("playlists/import")
    fields.update(name="Large file path import")
    fields.pop("file", None)
    answer = browser.http.post(
        "/shijhon/playlists/import",
        data=fields,
        files={"file": ("local.m3u8", source.encode(), "audio/x-mpegurl")},
    )
    assert answer.status_code == 303, answer.text
    ident = answer.headers["location"].rsplit("/", 1)[1]
    run = w.server.call(lambda: service.repo.runs(ident))[0]["id"]
    result = wait_state(w, run, ("preview", "failed"))
    assert result["state"] == "preview", result["error"]
    assert [s["id"] for s in result["data"]["selected"]] == [tagged["id"], untagged["id"]] * 501
    page = browser.get(f"playlists/import/{ident}")
    assert "1002 entries" in page.text and "Next 100" in page.text
    last_page = browser.get(f"playlists/import/{ident}?page=10")
    assert 'data-label="Entry">1002' in last_page.text
    original = service.client.call
    lost = False
    mutations: list[int] = []

    async def lost_append(connection: Connection, method: str, params: Any = ()) -> dict[str, Any]:
        nonlocal lost
        params = list(params)
        if method in ("createPlaylist", "updatePlaylist"):
            mutations.append(sum(k in ("songId", "songIdToAdd") for k, _ in params))
        result = await original(connection, method, params)
        if method == "updatePlaylist" and any(k == "songIdToAdd" for k, _ in params) and not lost:
            lost = True
            raise PlaylistError("Synthetic lost append response.")
        return result

    async def install() -> None:
        service.client.call = lost_append

    w.server.call(install)
    try:
        assert browser.submit(f"playlists/import/{ident}", {"action": "publish"}).status_code == 303
        result = wait_state(w, run, ("published", "failed", "uncertain"))
        assert result["state"] == "uncertain" and lost, result["error"]
        assert result["data"]["import_publish"]["pending"]["end"] == 400
        assert browser.submit(f"playlists/import/{ident}", {"action": "retry"}).status_code == 303
        result = wait_state(w, run, ("published", "failed", "uncertain"))
        assert result["state"] == "published", result["error"]
    finally:

        async def restore() -> None:
            service.client.call = original

        w.server.call(restore)
    assert max(mutations) <= 200 and sum(mutations) == 1002

    async def verify() -> None:
        connection = await connected_admin(service)
        mix = await service.repo.mix(ident, connection.user_id)
        playlist = await service.client.playlist(connection, mix["playlist_id"])
        assert [s["id"] for s in playlist["entry"]] == [tagged["id"], untagged["id"]] * 501

    w.server.call(verify)


def test_stock_navidrome_exported_m3u_reimports_paths_order_and_repeats(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    songs = w.nd.client().ok("search3", {"query": "", "songCount": 500})["searchResult3"]["song"]
    ids = [songs[0]["id"], songs[1]["id"], songs[0]["id"]]
    native = w.nd.client().ok(
        "createPlaylist", [("name", "Native synthetic export"), *(("songId", i) for i in ids)]
    )["playlist"]
    exported = httpx.get(
        f"{w.nd.base_url}/api/playlist/{native['id']}/tracks",
        headers={
            "accept": "audio/x-mpegurl",
            "x-nd-authorization": f"Bearer {w.nd.login()['token']}",
        },
    )
    assert exported.status_code == 200
    assert "#EXTM3U" in exported.text
    fields = browser.form("playlists/import")
    fields.update(name="Native export reimport")
    fields.pop("file", None)
    answer = browser.http.post(
        "/shijhon/playlists/import",
        data=fields,
        files={"file": ("native.m3u", exported.content, "audio/x-mpegurl")},
    )
    assert answer.status_code == 303, answer.text
    ident = answer.headers["location"].rsplit("/", 1)[1]
    run = w.server.call(lambda: service.repo.runs(ident))[0]["id"]
    result = wait_state(w, run, ("preview", "failed"))
    assert result["state"] == "preview", result["error"]
    assert [s["id"] for s in result["data"]["selected"]] == ids, (
        exported.text,
        result["data"]["rows"],
    )


@pytest.mark.parametrize("format", ["pls", "xspf", "jspf", "asx", "wpl", "csv", "tsv"])
def test_local_playlist_documents_resolve_actual_untagged_path_and_native_id(
    world: tuple[DeliveryWorld, Browser],
    format: str,
) -> None:
    import json
    from html import escape

    w, browser = world
    service = w.services.playlists
    assert service
    songs = w.nd.native("GET", "song", params={"library_id": 1, "_end": 500}).json()
    untagged = next(s for s in songs if "Useful Filename" in s["path"])
    path, ident = untagged["path"], untagged["id"]
    xml_path = escape(path, quote=True)
    source = {
        "pls": f"[playlist]\nFile1={path}\nFile2={path}\nNumberOfEntries=2\nVersion=2\n",
        "xspf": '<playlist xmlns="http://xspf.org/ns/0/" version="1"><trackList>'
        f"<track><location>{xml_path}</location></track>"
        f"<track><location>{xml_path}</location></track></trackList></playlist>",
        "jspf": json.dumps({"playlist": {"track": [{"location": [path]}, {"location": [path]}]}}),
        "asx": f'<ASX version="3.0"><ENTRY><REF HREF="{xml_path}"/></ENTRY>'
        f'<ENTRY><REF HREF="{xml_path}"/></ENTRY></ASX>',
        "wpl": f'<smil><body><seq><media src="{xml_path}"/>'
        f'<media src="{xml_path}"/></seq></body></smil>',
        "csv": f"native_id,rating,Artwork URL,Artwork_URL\n{ident},5,a,b\n{ident},4,c,d\n",
        "tsv": f"Location\tRating\n{path}\t5\n{path}\t4\n",
    }[format]
    fields = browser.form("playlists/import")
    fields.update(name="Synthetic document import")
    fields.pop("file", None)
    answer = browser.http.post(
        "/shijhon/playlists/import",
        data=fields,
        files={"file": (f"local.{format}", source.encode(), "application/octet-stream")},
    )
    assert answer.status_code == 303, answer.text
    import_id = answer.headers["location"].rsplit("/", 1)[1]
    run = w.server.call(lambda: service.repo.runs(import_id))[0]["id"]
    result = wait_state(w, run, ("preview", "failed"))
    assert result["state"] == "preview", result["error"]
    assert [s["id"] for s in result["data"]["selected"]] == [ident, ident]


def test_asx_bases_keep_remote_basenames_separate_from_actual_native_files(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    from html import escape
    from pathlib import PurePosixPath

    w, browser = world
    service = w.services.playlists
    assert service
    songs = w.nd.native("GET", "song", params={"library_id": 1, "_end": 500}).json()
    song = next(s for s in songs if "Useful Filename" in s["path"])
    path = PurePosixPath(song["path"])
    name = escape(path.name, quote=True)
    folder = escape("file:///" + str(path.parent).lstrip("/"), quote=True)
    source = (
        '<ASX><BASE HREF="https://example.invalid/remote/"/>'
        f'<ENTRY><REF HREF="{name}"/></ENTRY>'
        f'<REPEAT COUNT="2"><ENTRY><BASE HREF="{folder}"/>'
        f'<REF HREF="{name}"/></ENTRY></REPEAT></ASX>'
    )
    fields = browser.form("playlists/import")
    fields.update(name="Synthetic ASX bases")
    fields.pop("file", None)
    answer = browser.http.post(
        "/shijhon/playlists/import",
        data=fields,
        files={"file": ("scoped.asx", source.encode(), "application/xml")},
    )
    assert answer.status_code == 303, answer.text
    import_id = answer.headers["location"].rsplit("/", 1)[1]
    run = w.server.call(lambda: service.repo.runs(import_id))[0]["id"]
    result = wait_state(w, run, ("preview", "failed"))
    assert result["state"] == "preview", result["error"]
    assert [row["state"] for row in result["data"]["rows"]] == ["unresolved", "matched", "matched"]
    assert [s["id"] for s in result["data"]["selected"]] == [song["id"], song["id"]]


def test_preupgrade_publication_recovers_after_final_native_rename(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    ident, run = submit(
        world,
        lines="Glass Orchard — First Light\n"
        "Glass Orchard — Harbor (Live)\nGlass Orchard — First Light",
    )
    data = w.server.call(lambda: service.repo.run(run))["data"]
    ids = [s["id"] for s in data["selected"]]
    native = w.nd.client().ok(
        "createPlaylist", [("name", data["definition"]["name"]), *(("songId", i) for i in ids)]
    )["playlist"]
    data.update(
        native_ids=ids,
        created_id=native["id"],
        intent="create",
        provisional="Shijhon preparing synthetic-upgrade-window",
        import_review={"total": len(ids), "excluded": []},
    )
    assert "import_publish" not in data
    original_call = service.client.call
    mutations = []

    async def observed(connection: Connection, method: str, params: Any = ()) -> dict[str, Any]:
        if method in ("createPlaylist", "updatePlaylist"):
            mutations.append(method)
        return await original_call(connection, method, params)

    async def install() -> None:
        async with service.changes:
            service.client.call = observed
            await service.repo.update(run, "uncertain", data)

    w.server.call(install)
    try:
        assert browser.submit(f"playlists/import/{ident}", {"action": "retry"}).status_code == 303
        result = wait_state(w, run, ("published", "failed", "uncertain"))
        assert result["state"] == "published", result["error"]
        assert result["data"]["native_ids"] == ids
        saved = w.nd.client().ok("getPlaylist", {"id": native["id"]})["playlist"]
        assert [s["id"] for s in saved["entry"]] == ids
        assert mutations == []
    finally:

        async def restore() -> None:
            service.client.call = original_call

        w.server.call(restore)


@pytest.fixture
def crowded(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> Iterator[tuple[tuple[DeliveryWorld, Browser], str, list[Path]]]:
    """The release's artist with more albums than one lookup reads, among them an earlier
    album of the release's title with other songs."""
    nd = navidrome_factory()
    earlier = Album(
        RELEASE.artist,
        RELEASE.title,
        (Track("Earlier Song", 1), Track("Earlier Hymn", 2)),
        recording_date="2004",
    )
    paths = write_album(nd.music, earlier)
    for n in range(FILLERS):
        filler(nd.music, RELEASE.artist, f"Aa Filler {n:03d}", RELEASE.artist, f"f/{n}")
    nd.scan()
    listed = nd.client().ok("getAlbumList2", {"type": "alphabeticalByName", "size": "500"})
    album_id = next(a["id"] for a in listed["albumList2"]["album"] if a["name"] == RELEASE.title)
    catalog = Replay().catalog()

    async def search(term: str, limit: int = 20) -> SearchResults:
        return SearchResults(songs=tuple(s for s in SONGS if s.title in term))

    catalog.search = search  # type: ignore[method-assign]
    fill = {"library_pass": "dry_run", "auto_min_songs": 99999, "auto_min_share": 1.0}
    with delivery_world(nd, tmp_path / "world", catalog=catalog, fill=fill) as w:
        assert w.services.commits and w.services.commits.owned  # first use looks for albums
        browser = Browser(w.server.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        form = {"password": ADMIN_PASSWORD, "backend": "none"}
        assert browser.submit("playlists", form, "playlists/connection").status_code == 303
        yield (w, browser), album_id, paths
        browser.close()


def test_import_adds_a_release_beside_an_owned_album_of_its_title(
    crowded: tuple[tuple[DeliveryWorld, Browser], str, list[Path]],
) -> None:
    """An owned album of the release's title that is another album (other songs), by an
    artist whose albums fill a whole library lookup: publication adds the release as an
    album of its own and leaves the owned one as it is."""
    world, album_id, paths = crowded
    w, browser = world
    native = w.nd.client()
    lookup = {"query": RELEASE.artist, "albumCount": "100", "artistCount": "0", "songCount": "0"}
    assert len(native.ok("search3", lookup)["searchResult3"]["album"]) == 100
    before = native.ok("getAlbum", {"id": album_id})["album"]
    files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    ident, run = submit(
        world,
        csv="artist,title,album,version\n"
        + "\n".join(f"{s.artist},{s.title},{RELEASE.title}," for s in SONGS[:2]),
    )
    assert browser.submit(f"playlists/import/{ident}", {"action": "publish"}).status_code == 303
    result = wait_state(w, run, ("published", "failed", "uncertain"))
    assert result["state"] == "published", result["error"]
    songs = [native.ok("getSong", {"id": i})["song"] for i in result["data"]["native_ids"]]
    assert [s["title"] for s in songs] == [SONGS[0].title, SONGS[1].title]
    albums = {s["albumId"] for s in songs}
    assert len(albums) == 1 and album_id not in albums
    added = native.ok("getAlbum", {"id": albums.pop()})["album"]
    assert (added["name"], added["songCount"]) == (RELEASE.title, len(RELEASE.tracks))
    assert native.ok("getAlbum", {"id": album_id})["album"] == before
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == files
