"""Explicit native albums stay complete without changing personal library choices."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import closing
from typing import Any

import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.collection_app import CollectionApp
from tests.harness.library import Album, Track, simple_album, write_album
from tests.harness.navidrome import NavidromeInstance
from tests.harness.running import RunningServer, running

PASSWORD = "invented-album-password"
Params = Sequence[tuple[str, str]]


class AlbumWorld:
    def __init__(
        self,
        nd: NavidromeInstance,
        app: CollectionApp,
        server: RunningServer,
        users: dict[str, str],
        foreign: int,
    ) -> None:
        self.nd, self.app, self.server, self.users, self.foreign = nd, app, server, users, foreign
        self.tokens = {name: nd.login(name, PASSWORD)["token"] for name in users}
        self.album = next(s["albumId"] for s in app.songs if s["album"] == "Complete Chapters")
        self.other = next(s["albumId"] for s in app.songs if s["album"] == "Other Chapters")
        other_songs = sorted(s["id"] for s in app.songs if s["albumId"] == self.other)
        self.chosen = set(other_songs[:2])
        self.counted = {sorted(app.originals)[0], *other_songs[:3]}
        hidden = next(
            s["id"]
            for s in app.songs
            if s["albumId"] == self.album and s["id"] not in app.originals
        )
        with closing(nd.client("first", PASSWORD)) as client:
            self.playlist = client.ok(
                "createPlaylist",
                {
                    "name": "Explicit album references",
                    "songId": [hidden, sorted(app.originals)[0], hidden],
                },
            )["playlist"]["id"]

    def read(
        self,
        resource: str,
        params: Params = (),
        *,
        user: str = "first",
        direct: bool = False,
        method: str = "GET",
        token: str | None = None,
    ) -> httpx.Response:
        deadline = time.monotonic() + 8
        while True:
            response = httpx.request(
                method,
                (self.nd.base_url if direct else self.server.base_url) + "/api/" + resource,
                params=params,
                headers={
                    "x-nd-authorization": "Bearer " + (token or self.tokens[user]),
                    "accept-encoding": "identity",
                },
                timeout=10,
            )
            if (
                response.status_code != 503
                or response.headers.get("x-shijhon-view-state") != "indexing"
                or time.monotonic() >= deadline
            ):
                return response
            time.sleep(0.05)

    def parity(self, resource: str, params: Params = (), **kwargs: Any) -> httpx.Response:
        native = self.read(resource, params, direct=True, **kwargs)
        actual = self.read(resource, params, **kwargs)
        assert actual.status_code == native.status_code, actual.text
        assert actual.content == native.content
        for header in ("content-type", "x-total-count", "cache-control", "etag"):
            assert actual.headers.get(header) == native.headers.get(header), header
        assert "x-shijhon-indexed-at" not in actual.headers
        return actual

    def snapshot(self) -> tuple[str, dict[str, str]]:
        # The query cache is separate: browsing may populate it, but no application
        # membership, profiles, history, provenance or publication state may change.
        with sqlite3.connect(
            f"file:{self.app.state / 'collection.sqlite3'}?mode=ro", uri=True
        ) as db:
            state = "\n".join(db.iterdump())
        media = {
            str(path.relative_to(self.nd.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for root in (self.nd.music, self.nd.root / "foreign-music")
            for path in root.rglob("*")
            if path.is_file()
        }
        return state, media


@pytest.fixture(scope="module")
def albums(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AlbumWorld]:
    nd = navidrome_factory()
    write_album(
        nd.music,
        Album(
            "Invented Narrator",
            "Complete Chapters",
            tuple(
                Track(f"Chapter {i + 1:02d}", i % 8 + 1, disc=i // 8 + 1, seconds=3.7)
                for i in range(15)
            ),
        ),
    )
    write_album(nd.music, simple_album("Another Narrator", "Other Chapters", 4))
    write_album(nd.music, simple_album("Unused Narrator", "Unselected Chapters", 6))
    foreign_path = nd.root / "foreign-music"
    foreign_path.mkdir()
    write_album(foreign_path, simple_album("Foreign Narrator", "Foreign Chapters", 3))
    foreign = int(
        nd.native(
            "POST",
            "library",
            json={"name": "Foreign fixture", "path": str(foreign_path)},
        ).json()["id"]
    )
    nd.scan(full=True)
    songs = nd.native("GET", "song", params={"library_id": 1, "_sort": "id", "_end": 100}).json()
    users = {name: nd.create_user(name, PASSWORD) for name in ("first", "second", "grants")}
    app = CollectionApp(nd, tmp_path_factory.mktemp("native-album-discovery"), songs, wait=2)
    app.originals = {
        s["id"]
        for s in sorted(
            (s for s in songs if s["album"] == "Complete Chapters"),
            key=lambda s: s["title"],
        )[:7]
    }
    with running(app) as server:
        world = AlbumWorld(nd, app, server, users, foreign)

        async def profiles() -> None:
            for owner, profile in (
                (users["first"], "partial"),
                (users["first"], "empty"),
                (users["first"], "counted"),
                (users["second"], "other"),
            ):
                await app.store.execute(
                    "INSERT INTO collection_profiles(id,owner_id,name,base_original,created) "
                    "VALUES (?,?,?,0,1)",
                    [profile, owner, profile],
                )
            # Custom libraries hold their hand choices only, and a song added by hand
            # brings its album: each holds its songs, the rest of their albums removed.
            for owner, profile, chosen in (
                (users["first"], "partial", world.app.originals),
                (users["first"], "counted", world.counted),
                (users["second"], "other", world.chosen),
            ):
                albums = {s["albumId"] for s in songs if s["id"] in chosen}
                for song in (s["id"] for s in songs if s["albumId"] in albums):
                    await app.store.execute(
                        "INSERT INTO collection_membership VALUES (?,?,'1',?,?,1,'fixture')",
                        [owner, profile, "native:" + song, int(song in chosen)],
                    )
            for user, profile in (("first", "partial"), ("second", "other")):
                await app.store.execute(
                    "INSERT INTO collection_preferences VALUES (?,?,0)",
                    [users[user], profile],
                )

        server.call(profiles)
        yield world


def album_metadata(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Native image-cache annotations appear asynchronously after a candidate fetch.
    # Every other native field, including complete counts/duration/size, stays exact.
    return [{k: v for k, v in row.items() if not k.startswith("image")} for row in rows]


def selected_lists(world: AlbumWorld, user: str, selected: set[str]) -> None:
    for filters in (
        [],
        [("album_id", "")],
        [("album_id", ""), ("album_id", "")],
    ):
        native = world.read(
            "song", [*filters, ("library_id", "1"), ("_end", "100")], user=user, direct=True
        )
        expected_songs = {s["id"] for s in native.json()} & selected
        for sort in ("id", "random"):
            response = world.read(
                "song",
                [
                    *filters,
                    ("library_id", "1"),
                    ("_sort", sort),
                    ("seed", "fixture-seed"),
                    ("_start", "0"),
                    ("_end", "100"),
                ],
                user=user,
            )
            assert response.status_code == 200, response.text
            assert {s["id"] for s in response.json()} == expected_songs
            assert response.headers["x-total-count"] == str(len(expected_songs))
    listing = world.read(
        "album", [("library_id", "1"), ("_sort", "id"), ("_end", "100")], user=user
    )
    assert listing.status_code == 200, listing.text
    visible = {s["albumId"] for s in world.app.songs if s["id"] in selected}
    native = world.read(
        "album", [("library_id", "1"), ("_sort", "id"), ("_end", "100")], user=user, direct=True
    )
    expected = [album for album in native.json() if album["id"] in visible]
    assert album_metadata(listing.json()) == album_metadata(expected)
    assert listing.headers["x-total-count"] == str(len(expected))
    shuffled = world.read(
        "album",
        [
            ("library_id", "1"),
            ("_sort", "random"),
            ("seed", "fixture-seed"),
            ("_end", "100"),
        ],
        user=user,
    )
    assert shuffled.status_code == 200, shuffled.text
    assert {a["id"]: a for a in album_metadata(shuffled.json())} == {
        a["id"]: a for a in album_metadata(expected)
    }
    assert shuffled.headers["x-total-count"] == str(len(expected))


@pytest.mark.parametrize("profile", ["partial", "empty", "other", "counted"])
def test_full_album_details_and_pages_preserve_selected_lists_and_state(
    albums: AlbumWorld,
    profile: str,
) -> None:
    w = albums
    user = "second" if profile == "other" else "first"
    w.server.call(
        lambda: w.app.store.execute(
            "UPDATE collection_preferences SET active_profile_id=? WHERE owner_id=?",
            [profile, w.users[user]],
        )
    )
    selected = (
        w.chosen
        if profile == "other"
        else w.app.originals
        if profile == "partial"
        else w.counted
        if profile == "counted"
        else set()
    )
    before = w.snapshot()
    selected_lists(w, user, selected)
    album = w.parity("album/" + w.album, user=user).json()
    assert album["songCount"] == 15
    assert album["duration"] > 15 * 3
    for order in ("ASC", "DESC"):
        whole = w.read(
            "song",
            [
                ("album_id", w.album),
                ("_sort", "album"),
                ("_order", order),
                ("_end", "100"),
            ],
            user=user,
            direct=True,
        ).json()
        assert len(whole) == 15
        pages = []
        for start in range(0, 20, 4):
            page = w.parity(
                "song",
                [
                    ("album_id", w.album),
                    ("_sort", "album"),
                    ("_order", order),
                    ("_start", str(start)),
                    ("_end", str(start + 4)),
                ],
                user=user,
            )
            assert page.headers["x-total-count"] == "15"
            pages.extend(page.json())
        assert [s["id"] for s in pages] == [s["id"] for s in whole]
    hidden = next(
        s["id"] for s in w.app.songs if s["albumId"] == w.album and s["id"] not in w.app.originals
    )
    w.parity("song/" + hidden, user=user)
    w.parity("song", [("id", hidden), ("id", sorted(w.app.originals)[0])], user=user)
    w.parity("playlist/" + w.playlist + "/tracks", user=user)
    selected_lists(w, user, selected)
    assert w.snapshot() == before


def test_repeated_explicit_album_filters_keep_native_order_and_totals(albums: AlbumWorld) -> None:
    w = albums
    for first, second in (
        (w.album, w.other),
        (w.other, w.album),
        (w.album, w.album),
        ("unknown-album", w.album),
        (w.album, "unknown-album"),
        ("", w.album),
        (w.album, ""),
    ):
        result = w.parity(
            "song",
            [
                ("album_id", first),
                ("album_id", second),
                ("_sort", "album"),
                ("_end", "100"),
            ],
        )
        assert result.status_code == 200
        assert len(result.json()) == (19 if w.other in (first, second) else 15)


def test_native_album_count_sorts_and_warm_membership_keep_complete_metadata(
    albums: AlbumWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = albums
    w.server.call(
        lambda: w.app.store.execute(
            "UPDATE collection_preferences SET active_profile_id='counted' WHERE owner_id=?",
            [w.users["first"]],
        )
    )
    fetches = []
    original = w.app.native._json

    async def observe(
        path: bytes, pairs: list[tuple[str, str]], headers: list[tuple[bytes, bytes]]
    ) -> tuple[Any, list[tuple[bytes, bytes]]]:
        if path == b"/api/album" and ("library_id", "1") in pairs:
            fetches.append(list(pairs))
        return await original(path, pairs, headers)

    monkeypatch.setattr(w.app.native, "_json", observe)
    selected = set(w.counted)

    def pages() -> None:
        visible = {s["albumId"] for s in w.app.songs if s["id"] in selected}
        for sort in ("song_count", "duration", "size", "recently_added"):
            for order in ("ASC", "DESC"):
                params = [("library_id", "1"), ("_sort", sort), ("_order", order)]
                native = w.read("album", [*params, ("_end", "100")], direct=True)
                assert native.status_code == 200, native.text
                expected = [a for a in native.json() if a["id"] in visible]
                if sort in ("song_count", "duration") and len(visible) == 2:
                    assert [a["id"] for a in expected] == (
                        [w.other, w.album] if order == "ASC" else [w.album, w.other]
                    )
                for start in range(len(expected) + 1):
                    actual = w.read(
                        "album",
                        [
                            *params,
                            ("_start", str(start)),
                            ("_end", str(start + 1)),
                        ],
                    )
                    assert actual.status_code == 200, actual.text
                    assert album_metadata(actual.json()) == album_metadata(
                        expected[start : start + 1]
                    )
                    assert actual.headers["x-total-count"] == str(len(expected))

    before = w.snapshot()
    pages()
    assert w.snapshot() == before
    warmed = list(fetches)
    assert warmed
    removed = [s for s in w.counted if s not in w.app.originals]

    async def membership(saved: int) -> None:
        for song in removed:
            await w.app.store.execute(
                "UPDATE collection_membership SET saved=? WHERE owner_id=? "
                "AND profile_id='counted' AND track_key=?",
                [saved, w.users["first"], "native:" + song],
            )

    try:
        w.server.call(lambda: membership(0))
        selected.difference_update(removed)
        before = w.snapshot()
        pages()
        assert fetches == warmed  # live membership filters the existing candidate generation
        assert w.snapshot() == before
        w.server.call(lambda: membership(1))
        selected.update(removed)
        before = w.snapshot()
        pages()
        assert fetches == warmed
        assert w.snapshot() == before
    finally:
        w.server.call(lambda: membership(1))


@pytest.mark.parametrize("state", ["missing", "indexing"])
def test_explicit_albums_do_not_require_collection_readiness(
    albums: AlbumWorld, state: str
) -> None:
    w = albums

    async def readiness(generation: str, status: str, updated: int) -> None:
        await w.app.store.execute(
            "UPDATE collection_index SET generation=?,state=?,updated_at=? WHERE library_id='1'",
            [generation, status, updated],
        )

    w.server.call(lambda: readiness("", state, 0))
    try:
        before = w.snapshot()
        for method in ("GET", "HEAD"):
            for album in (w.album, "unknown-album"):
                w.parity("album/" + album, method=method)
                w.parity("song", [("album_id", album), ("_end", "4")], method=method)
        assert w.snapshot() == before
    finally:
        w.server.call(lambda: readiness("fixture", "ready", 1))


def test_direct_album_permissions_remain_native_after_grant_revocation(albums: AlbumWorld) -> None:
    w = albums
    foreign_album = w.nd.native("GET", "album", params={"library_id": w.foreign}).json()[0]["id"]
    for grants in ([1, w.foreign], [1], [w.foreign]):
        w.nd.native("PUT", "user/" + w.users["grants"] + "/library", json={"libraryIds": grants})
        before = w.snapshot()
        for album in (w.album, foreign_album, "unknown-album"):
            w.parity("album/" + album, user="grants")
            w.parity("song", [("album_id", album), ("_end", "100")], user="grants")
        w.parity("song", [("album_id", w.album), ("album_id", foreign_album)], user="grants")
        assert w.snapshot() == before
    for resource, params in (("album/" + w.album, []), ("song", [("album_id", w.album)])):
        refused = w.parity(resource, params, token=w.tokens["first"] + "invalid")
        assert refused.status_code == 401
