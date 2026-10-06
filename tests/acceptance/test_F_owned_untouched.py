"""Suite F — owned albums untouched.

Filling a partially owned album never changes how it looks: cover (file or embedded),
release type, genres, year, ``created`` and the "newest" order, and owned song IDs stay
as they were. Owned files are never written to.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from tests.harness.engine import catalog_release, engine_for
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import NavidromeInstance

pytestmark = pytest.mark.anyio

OWNED = {
    "cover-file": Album(
        "Keeper", "Kept Cover", (Track("Kept 1", 1), Track("Kept 2", 2)),
        cover_file=True, genre="Jazz", recording_date="2018",
    ),
    "embedded": Album(
        "Keeper", "Kept Embedded", (Track("Emb 1", 1), Track("Emb 2", 2)),
        embedded_cover=True, genre="Blues", recording_date="2017-02-03",
    ),
    "no-art": Album(
        "Keeper", "Kept Plain", (Track("Plain 1", 1),), fmt="mp3", recording_date="2016",
    ),
}  # fmt: skip


@pytest.fixture(scope="module")
def owned(navidrome: NavidromeInstance) -> dict[str, list[Path]]:
    files = {key: write_album(navidrome.music, album) for key, album in OWNED.items()}
    navidrome.scan(full=True)
    return files


def album_id(nd: NavidromeInstance, title: str) -> str:
    hits = nd.client().ok("search3", {"query": title, "albumCount": 10, "songCount": 0})
    [found] = [a["id"] for a in hits["searchResult3"]["album"] if a["name"] == title]
    return str(found)


def presentation(nd: NavidromeInstance, album: str) -> dict[str, Any]:
    admin = nd.client()
    detail = admin.ok("getAlbum", {"id": album})["album"]
    cover = admin.request("getCoverArt", {"id": detail.get("coverArt", f"al-{album}"), "size": 64})
    newest = admin.ok("getAlbumList2", {"type": "newest", "size": 50})["albumList2"]["album"]
    return {
        "coverArt": detail.get("coverArt"),
        "cover_bytes": hashlib.sha256(cover.content).hexdigest(),
        "releaseTypes": detail.get("releaseTypes"),
        "genres": detail.get("genres"),
        "genre": detail.get("genre"),
        "year": detail.get("year"),
        "created": detail.get("created"),
        "name": detail.get("name"),
        "artist": detail.get("artist"),
        "newest": [a["id"] for a in newest],
        "songs": sorted(s["id"] for s in detail["song"]),
    }


@pytest.mark.parametrize("key", list(OWNED))
async def test_fill_leaves_owned_presentation(
    navidrome: NavidromeInstance, owned: dict[str, list[Path]], tmp_path: Path, key: str
) -> None:
    album = OWNED[key]
    target = album_id(navidrome, album.title)
    before = presentation(navidrome, target)
    file_hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in owned[key]}
    release = catalog_release(f"f-{key}", album.title, album.artist, len(album.tracks) + 2)

    async with engine_for(navidrome, tmp_path) as parts:
        songs = (await parts.engine.owned_album(target)).songs
        by_number = {s["trackNumber"]: s["id"] for s in songs}
        links = {t.ref: by_number[t.number] for t in release.tracks if t.number in by_number}
        # A cover is offered but must not be written for an owned album.
        await parts.engine.materialize(
            release, owned_album_id=target, links=links, cover=b"\xff\xd8not-a-real-jpeg"
        )
        folder = parts.layout.absolute(
            parts.layout.release_folder(release.ref, release.artist, release.title)
        )
        assert not (folder / "cover.jpg").exists()

    after = presentation(navidrome, target)
    new_songs = set(after.pop("songs")) - set(before["songs"])
    assert len(new_songs) == 2
    before.pop("songs")
    assert after == before
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in owned[key]} == file_hashes
