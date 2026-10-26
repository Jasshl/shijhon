"""Importing music the library already has: apps show every song and album once.

Against a real, disposable Navidrome with invented music and two accounts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from shijhon.collection.origins import Origins
from shijhon.collection.reader import CallerLibrary
from shijhon.collection.service import Collection
from shijhon.music_imports import ImportError as ImportError_
from shijhon.music_imports import ImportLimits, ManifestEntry, MusicImports
from shijhon.music_imports.overlap import SAME_BYTES
from shijhon.music_imports.removal import ImportRemovals
from shijhon.navidrome.checks import PlaceholderWrites
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.navidrome.usage import UsageSource
from shijhon.placeholders.tags import album_tags_of
from shijhon.store import Store
from tests.harness.library import Album, Track, write_album
from tests.harness.music_import_formats import IMPORT_FORMATS, import_tone
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance

# Navidrome's album grouping decides whether a song joins an album: a canary.
pytestmark = [pytest.mark.anyio, pytest.mark.canary]

ARTIST, TITLE = "Tide Paper", "Night Letters"
LANTERNS = tuple(Track(f"Lantern {n}", n, seconds=1.0 + n / 10) for n in range(1, 7))
LIBRARY = Album(ARTIST, TITLE, LANTERNS[:4], folder="Library/Tide Paper/Night Letters")


@dataclass
class World:
    nd: NavidromeInstance
    store: Store
    caller: CallerLibrary
    collection: Collection
    imports: MusicImports
    removals: ImportRemovals
    users: dict[str, tuple[str, str]]  # name -> (id, password)

    def client(self, name: str) -> Any:
        return self.nd.client(name, self.users[name][1])

    async def call(self, name: str, method: str, params: dict[str, Any]) -> Any:
        client = self.client(name)
        try:
            return await anyio.to_thread.run_sync(lambda: client.ok(method, params))
        finally:
            client.close()


async def world(nd: NavidromeInstance, tmp_path: Path, names: tuple[str, ...]) -> World:
    native = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD)
    caller = CallerLibrary(SimpleNamespace(client=SimpleNamespace(native=native)))
    users = {}
    for name in names:
        ident = nd.create_user(name, "invented-password-" + name)
        login = nd.login(name, "invented-password-" + name)
        caller.remember(await caller.access.verify_native(login["token"]), login["token"])
        users[name] = (ident, "invented-password-" + name)
    store = await Store.open(tmp_path / "state.db")
    collection, scans = Collection(store, caller), ScanCoordinator(native)
    (nd.music / "_imports").mkdir(exist_ok=True)
    imports = MusicImports(
        store,
        caller,
        library_id="1",
        library_path=nd.music,
        limits=replace(ImportLimits(), min_free_bytes=0),
        scan=scans.scan,
        native_lookup=caller.imported_file,
        refresh=collection.refresh,
        native_song=caller.song,
    )
    collection.index.asset_lookup = imports.asset_lookup
    writes = PlaceholderWrites(native)

    async def gate() -> None:
        assert await writes.refused(fresh=True) is None

    async def administrator(ident: str) -> bool:
        return False

    removals = ImportRemovals(
        imports,
        usage=UsageSource(nd.data / "navidrome.db", export=False),
        gate=gate,
        administrator=administrator,
        native_absent=caller.imported_file_absent,
    )
    await Origins(store).initialize_personal("1")
    await imports.initialize()
    await scans.scan([p.name for p in nd.music.iterdir() if p.is_dir() and p.name != "_imports"])
    for ident, _ in users.values():
        await collection.refresh(ident, "1")
    return World(nd, store, caller, collection, imports, removals, users)


async def upload(w: World, name: str, files: list[Path], root: Path, key: str) -> dict[str, Any]:
    """Upload and check ``files``; returns the frozen review."""
    owner = w.users[name][0]
    job = await w.imports.create(owner, key)
    entries = [
        ManifestEntry(f"f{n}", str(f.relative_to(root)), f.stat().st_size)
        for n, f in enumerate(files)
    ]
    job = await w.imports.declare(owner, job["id"], "page", entries)
    await w.imports.seal(owner, job["id"])
    by_key = {f"f{n}": f for n, f in enumerate(files)}
    for item in job["items"]:
        data = by_key[item["client_key"]].read_bytes()

        async def stream(data: bytes = data) -> Any:
            yield data

        await w.imports.receive(owner, job["id"], item["id"], 0, stream())
    await w.imports.review(owner, job["id"])
    return await settle(w, name, job["id"], "review")


async def settle(w: World, name: str, job_id: str, state: str) -> dict[str, Any]:
    owner = w.users[name][0]
    for _ in range(200):
        status = await w.imports.status(owner, job_id, limit=500)
        if status["state"] == state:
            return status
        assert status["state"] not in ("paused", "cancelled"), status
        assert await w.imports.tick(), status
    pytest.fail(f"The import did not reach {state}")


async def approve(w: World, name: str, review: dict[str, Any]) -> dict[str, Any]:
    owner = w.users[name][0]
    await w.imports.approve(owner, review["id"], review["revision"], "go-" + review["id"])
    return await settle(w, name, review["id"], "done")


def by_path(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["path"]: item for item in status["items"]}


async def album_songs(w: World, name: str, title: str = TITLE) -> list[dict[str, Any]]:
    found = await w.call(name, "search3", {"query": title, "albumCount": 20, "songCount": 0,
                                            "artistCount": 0})  # fmt: skip
    albums = [a for a in found["searchResult3"].get("album", []) if a["name"] == title]
    assert len(albums) == 1, albums  # never a second album of that name
    detail = await w.call(name, "getAlbum", {"id": albums[0]["id"]})
    return list(detail["album"].get("song", []))


async def test_overlapping_imports_show_each_song_and_album_once(navidrome_factory, tmp_path):
    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ana", "ben"))
    ana, ben = w.users["ana"][0], w.users["ben"][0]
    library = {s["title"]: s["id"] for s in await album_songs(w, "ana")}
    source = tmp_path / "ben"
    # Ben's copy: the same four songs as MP3, Lantern 5 with the same album tags, Lantern 6
    # with a release date only in his files, and two songs of the Deluxe Edition.
    same = write_album(source, replace(LIBRARY, tracks=LANTERNS[:5], fmt="mp3", folder="Same"))
    dated = write_album(
        source,
        replace(LIBRARY, tracks=LANTERNS[5:], release_date="2020-03-01", folder="Dated"),
    )
    deluxe = write_album(
        source,
        replace(LIBRARY, title=TITLE + " (Deluxe Edition)", tracks=LANTERNS[:2], folder="Deluxe"),
    )
    review = await upload(w, "ben", same + dated + deluxe, source, "ben-import")
    items = by_path(review)
    outcomes = {path.split("/")[0] + ":" + path.rsplit(" ", 1)[-1]: i["outcome"]
                for path, i in items.items()}  # fmt: skip
    assert outcomes == {
        "Same:1.mp3": "linked",
        "Same:2.mp3": "linked",
        "Same:3.mp3": "linked",
        "Same:4.mp3": "linked",
        "Same:5.mp3": "new",
        "Dated:6.flac": "copy",
        "Deluxe:1.flac": "new",
        "Deluxe:2.flac": "new",
    }, outcomes
    linked = items["Same/1-01 Lantern 1.mp3"]
    assert linked["match"]["song_id"] == library["Lantern 1"]
    assert linked["match"]["library_quality"].startswith("FLAC")
    assert linked["match"]["quality"].startswith("MP3")
    assert review["linked_files"] == 4 and review["copy_files"] == 1
    assert review["eligible_files"] == 4  # Lantern 5, the copy of Lantern 6, two Deluxe songs
    done = await approve(w, "ben", review)
    assert done["complete_files"] == 4 and done["linked_files"] == 4

    # One Night Letters with all six songs, each once, in Navidrome's own album answer too.
    for name in ("ana", "ben"):
        songs = await album_songs(w, name)
        assert sorted(s["title"] for s in songs) == [f"Lantern {n}" for n in range(1, 7)]
        assert {s["id"] for s in songs} >= set(library.values())
    artist = await w.call("ben", "getArtist", {"id": songs[0]["artistId"]})
    assert sorted(a["name"] for a in artist["artist"]["album"]) == [
        TITLE,
        TITLE + " (Deluxe Edition)",
    ]
    album_id = songs[0]["albumId"]
    rows = await w.caller._read(  # the native website's album page
        "/api/song",
        params={"album_id": album_id, "_start": 0, "_end": 50, "missing": "false"},
        maximum=1024**2,
        headers={"x-nd-authorization": "Bearer " + w.caller._token(ben)},
    )
    assert sorted(r["title"] for r in json.loads(rows[0])) == [f"Lantern {n}" for n in range(1, 7)]

    # The copy carries the library album's tags; Ben's original stays as he sent it.
    copy_row = await w.store.fetchone("SELECT * FROM music_import_items WHERE derived_from<>''")
    original_row = await w.store.fetchone(
        "SELECT * FROM music_import_items WHERE id=?", [copy_row["derived_from"]]
    )
    library_tags = album_tags_of(nd.music / "Library/Tide Paper/Night Letters/1-01 Lantern 1.flac")
    assert album_tags_of(nd.music / "_imports" / copy_row["final_name"]) == library_tags
    retained = nd.music / "_imports" / ".incoming" / original_row["staging_name"]
    assert (
        hashlib.sha256(retained.read_bytes()).hexdigest()
        == hashlib.sha256(dated[0].read_bytes()).hexdigest()
    )
    assert original_row["state"] == "joined"
    # A linked file's bytes are not kept.
    linked_row = await w.store.fetchone(
        "SELECT * FROM music_import_items WHERE id=?", [linked["id"]]
    )
    assert linked_row["state"] == "linked" and linked_row["reserved_size"] == 0
    assert not (nd.music / "_imports" / ".incoming" / linked_row["staging_name"]).exists()

    # The linked songs count as Ben's own imports; Ana gets nothing from Ben's import.
    all_ids = [s["id"] for s in songs]
    mine = await w.collection.visible_song_ids(ben, "1", all_ids, mode="original")
    assert mine == set(all_ids)
    assert await w.collection.visible_song_ids(ana, "1", all_ids, mode="original") == set()

    # Removing Ben's import takes his files and his links out of his own imports only.
    await w.removals.review(ben, done["id"])
    status = await advance(w.removals, ben, done["id"], "review")
    assert status["linked_songs"] == 4
    await w.removals.approve(ben, done["id"], status["fingerprint"], "remove-ben")
    await advance(w.removals, ben, done["id"], "removed")
    await w.collection.refresh(ben, "1")
    left = await w.collection.visible_song_ids(ben, "1", list(library.values()), mode="original")
    assert left == set()
    assert sorted(s["title"] for s in await album_songs(w, "ana")) == [
        f"Lantern {n}" for n in range(1, 5)
    ]
    await w.removals.restore(ben, done["id"])
    await advance(w.removals, ben, done["id"], "restored")
    await w.collection.refresh(ben, "1")
    back = await w.collection.visible_song_ids(ben, "1", list(library.values()), mode="original")
    assert back == set(library.values())
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def advance(removals: ImportRemovals, owner: str, job: str, wanted: str) -> dict[str, Any]:
    for _ in range(40):
        status = await removals.status(owner, job)
        assert not status.get("error"), status
        if status["state"] == wanted:
            return status
        assert await removals.tick(), status
    pytest.fail(f"Removal did not reach {wanted}")


async def test_concurrent_imports_of_one_album_link_the_later_one(navidrome_factory, tmp_path):
    nd = navidrome_factory()
    w = await world(nd, tmp_path, ("ana", "ben"))
    album = Album("Glass Orchard", "Paper Garden", LANTERNS[:3], folder="Paper Garden")
    first = write_album(tmp_path / "ana", album)
    second = write_album(tmp_path / "ben", replace(album, fmt="mp3"))
    # Both reviews finish before either import is published.
    review_a = await upload(w, "ana", first, tmp_path / "ana", "ana-import")
    review_b = await upload(w, "ben", second, tmp_path / "ben", "ben-import")
    assert {i["outcome"] for i in review_a["items"]} == {"new"}
    assert {i["outcome"] for i in review_b["items"]} == {"new"}
    await w.imports.approve(w.users["ana"][0], review_a["id"], review_a["revision"], "go-a")
    await w.imports.approve(w.users["ben"][0], review_b["id"], review_b["revision"], "go-b")
    await settle(w, "ana", review_a["id"], "done")
    done = await settle(w, "ben", review_b["id"], "done")
    assert done["linked_files"] == 3 and done["complete_files"] == 0
    assert all("Another import added this song" in " ".join(i["warnings"]) for i in done["items"])
    songs = await album_songs(w, "ben", "Paper Garden")
    assert sorted(s["title"] for s in songs) == ["Lantern 1", "Lantern 2", "Lantern 3"]
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_catalog_songs_and_not_the_same_song(navidrome_factory, tmp_path):
    nd = navidrome_factory()
    # A catalog song Shijhon added: a file in the managed placeholder folder.
    catalog = replace(LIBRARY, tracks=LANTERNS[:2], folder="_shijhon/Tide Paper/Night Letters")
    write_album(nd.music, catalog)
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    await w.collection.refresh(ben, "1")
    source = tmp_path / "ben"
    files = write_album(source, replace(LIBRARY, tracks=LANTERNS[:2], fmt="mp3", folder="Mine"))
    review = await upload(w, "ben", files, source, "ben-import")
    assert {i["outcome"] for i in review["items"]} == {"catalog_song"}
    # "Not the same song": the review runs again and publishes that file as it is.
    first = sorted(review["items"], key=lambda i: i["path"])[0]
    await w.imports.distinct(ben, review["id"], first["id"])
    again = await settle(w, "ben", review["id"], "review")
    assert again["revision"] > review["revision"]
    outcomes = sorted((i["path"], i["outcome"], i["distinct"]) for i in again["items"])
    assert outcomes == [
        ("Mine/1-01 Lantern 1.mp3", "new", True),
        ("Mine/1-02 Lantern 2.mp3", "catalog_song", False),
    ]
    done = await approve(w, "ben", again)
    link = await w.store.fetchone("SELECT * FROM music_import_links")
    assert link["kind"] == "catalog_song" and link["active"] == 1
    # The catalog song counts as Ben's own import; its received file is retained.
    linked = next(i for i in done["items"] if i["outcome"] == "catalog_song")
    row = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [linked["id"]])
    assert (nd.music / "_imports" / ".incoming" / row["staging_name"]).exists()
    assert await w.collection.visible_song_ids(ben, "1", [link["song_id"]], mode="original") == {
        link["song_id"]
    }
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_reviewing_again_discards_copies_and_makes_them_once(navidrome_factory, tmp_path):
    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    source = tmp_path / "ben"
    dated = write_album(
        source, replace(LIBRARY, tracks=LANTERNS[4:6], release_date="2020-03-01", folder="D")
    )
    review = await upload(w, "ben", dated, source, "ben-import")
    assert [i["outcome"] for i in review["items"]] == ["copy", "copy"]
    incoming = nd.music / "_imports" / ".incoming"
    copies = {r["staging_name"] for r in await w.store.fetchall(
        "SELECT staging_name FROM music_import_items WHERE derived_from<>''")}  # fmt: skip
    assert len(copies) == 2 and copies <= {p.name for p in incoming.iterdir()}
    # Excluding a file reviews everything again: the earlier copies go first.
    excluded, kept_path = review["items"][0]["id"], review["items"][1]["path"]
    await w.imports.review(ben, review["id"], exclude=[excluded])
    again = await settle(w, "ben", review["id"], "review")
    assert sorted(i["state"] for i in again["items"]) == ["excluded", "joined"]
    rows = await w.store.fetchall("SELECT * FROM music_import_items WHERE derived_from<>''")
    assert len(rows) == 1
    names = {p.name for p in incoming.iterdir()}
    # The earlier copies are gone; the kept file's is made anew, under a name of its own.
    assert not copies & names and rows[0]["staging_name"] in names
    originals = await w.store.fetchall(
        "SELECT staging_name FROM music_import_items WHERE derived_from=''"
    )
    assert names - {".ndignore"} == {rows[0]["staging_name"], *(o[0] for o in originals)}
    original = await w.store.fetchone(
        "SELECT final_name FROM music_import_items WHERE id=?", [excluded]
    )
    assert original["final_name"] == "batch-" + review["id"] + "/" + review["items"][0]["path"]
    done = await approve(w, "ben", again)
    assert done["complete_files"] == 1
    kept = "Lantern " + kept_path.rsplit(" ", 1)[1].removesuffix(".flac")  # "D/1-06 Lantern 6.flac"
    assert sorted(s["title"] for s in await album_songs(w, "ben")) == [
        *(f"Lantern {n}" for n in range(1, 5)),
        kept,
    ]
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def approved(w: World, job_id: str) -> bool:
    row = await w.store.fetchone(
        "SELECT approved_revision FROM music_import_jobs WHERE id=?", [job_id]
    )
    return row["approved_revision"] is not None


def _isrc(n: int) -> str:
    return f"XXA0L20000{n:02d}"


ISRC_LANTERNS = tuple(replace(t, isrc=_isrc(t.number)) for t in LANTERNS)


async def test_native_recording_facts_and_music_added_since_the_last_refresh(
    navidrome_factory, tmp_path
):
    nd = navidrome_factory()
    write_album(nd.music, replace(LIBRARY, tracks=ISRC_LANTERNS[:3]))
    w = await world(nd, tmp_path, ("ben",))
    # Added to the folder and scanned after Shijhon's last refresh.
    late = Album("Glass Orchard", "Paper Garden", LANTERNS[:2], folder="Library/Paper Garden")
    write_album(nd.music, late)
    await anyio.to_thread.run_sync(nd.scan)
    source = tmp_path / "ben"
    other = replace(ISRC_LANTERNS[0], isrc="XXA0L2000099")  # another recording of its title
    files = write_album(
        source, replace(LIBRARY, tracks=(other, ISRC_LANTERNS[1]), fmt="mp3", folder="S")
    )
    files += write_album(source, replace(late, fmt="mp3", folder="L"))
    review = await upload(w, "ben", files, source, "ben-import")
    assert {p: i["outcome"] for p, i in by_path(review).items()} == {
        "S/1-01 Lantern 1.mp3": "new",
        "S/1-02 Lantern 2.mp3": "linked",
        "L/1-01 Lantern 1.mp3": "linked",
        "L/1-02 Lantern 2.mp3": "linked",
    }
    await approve(w, "ben", review)
    assert len(await album_songs(w, "ben", "Paper Garden")) == 2
    assert sorted(s["title"] for s in await album_songs(w, "ben")) == [
        "Lantern 1",
        "Lantern 1",
        "Lantern 2",
        "Lantern 3",
    ]
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_a_compilation_without_an_album_artist_is_linked_as_one_album(
    navidrome_factory, tmp_path
):
    nd = navidrome_factory()
    tracks = (
        Track("Gull", 1, seconds=1.2, artists=("Ada Quill",)),
        Track("Tern", 2, seconds=1.4, artists=("Bo Marsh",)),
    )
    comp = Album("Harbor Crew", "Harbor Lights", tracks, compilation=True,
                 write_album_artist=False, folder="Library/Harbor Lights")  # fmt: skip
    write_album(nd.music, comp)
    w = await world(nd, tmp_path, ("ben",))
    source = tmp_path / "ben"
    files = write_album(source, replace(comp, fmt="mp3", folder="C"))
    review = await upload(w, "ben", files, source, "ben-import")
    assert {i["outcome"] for i in review["items"]} == {"linked"}
    await approve(w, "ben", review)
    songs = await album_songs(w, "ben", "Harbor Lights")
    assert sorted(s["title"] for s in songs) == ["Gull", "Tern"]
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_a_link_is_checked_again_right_before_it_is_made(navidrome_factory, tmp_path):
    from mutagen.flac import FLAC

    nd = navidrome_factory()
    owned = write_album(nd.music, replace(LIBRARY, tracks=ISRC_LANTERNS[:3]))
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    source = tmp_path / "ben"
    files = write_album(source, replace(LIBRARY, tracks=ISRC_LANTERNS[:2], fmt="mp3", folder="S"))
    review = await upload(w, "ben", files, source, "ben-import")
    assert {i["outcome"] for i in review["items"]} == {"linked"}
    before = {s["title"]: s["id"] for s in await album_songs(w, "ben")}
    # After the review: Lantern 1's file becomes another recording (its ID stays: the ISRC
    # is not part of it) and Lantern 2's file goes.
    retagged = FLAC(owned[0])
    retagged["isrc"] = ["XXA0L2000098"]
    retagged.save()
    owned[1].unlink()
    await anyio.to_thread.run_sync(lambda: nd.scan(full=True))
    after = {s["title"]: s["id"] for s in await album_songs(w, "ben")}
    assert after["Lantern 1"] == before["Lantern 1"] and "Lantern 2" not in after
    await w.imports.approve(ben, review["id"], review["revision"], "go")
    done = await settle(w, "ben", review["id"], "done")
    assert done["complete_files"] == 2 and done["linked_files"] == 0
    assert all("was removed after the review" in " ".join(i["warnings"]) for i in done["items"])
    assert sorted(s["title"] for s in await album_songs(w, "ben")) == [
        "Lantern 1",
        "Lantern 1",
        "Lantern 2",
        "Lantern 3",
    ]
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_exactly_the_bytes_of_an_earlier_import_are_linked_by_their_receipt(
    navidrome_factory, tmp_path
):
    """Exactly the bytes another import published are linked by that import's receipt, not
    by their tags: an untagged MP3 (Navidrome titles it by its file name) and a Shorten file
    (no tags, no length the probe reads) - the later import finishes."""
    nd = navidrome_factory()
    w = await world(nd, tmp_path, ("ana", "ben"))
    source = tmp_path / "files"
    files = [
        import_tone(source / fmt.key, fmt, tagged=False)
        for fmt in IMPORT_FORMATS
        if fmt.key in ("mp3", "shorten")
    ]
    first = await approve(w, "ana", await upload(w, "ana", files, source, "ana-import"))
    assert first["complete_files"] == 2
    review = await upload(w, "ben", files, source, "ben-import")
    assert {i["outcome"] for i in review["items"]} == {"linked"}
    assert {i["match"]["reason"] for i in review["items"]} == {SAME_BYTES}
    done = await approve(w, "ben", review)
    assert done["linked_files"] == 2 and done["complete_files"] == 0, done
    songs = {i["path"]: i["song_id"] for i in first["items"]}
    assert {i["match"]["song_id"] for i in done["items"]} == set(songs.values())
    assert {i["state"] for i in done["items"]} == {"linked"}
    assert not [w for i in done["items"] for w in i["warnings"] if "review" in w]
    assert len(list((nd.music / "_imports").rglob("*.mp3"))) == 1
    assert len(list((nd.music / "_imports").rglob("*.shn"))) == 1
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("put", ["the same bytes", "other bytes", "a link"])
async def test_an_earlier_imports_file_put_back_in_its_place_is_checked_by_its_bytes(
    navidrome_factory, tmp_path, put
):
    """The untagged file an earlier import published, replaced at its path by another file
    of its size: with exactly the same bytes, importing them again links them to its song -
    never a second file; with other bytes, or with something that is not a file there (a
    link), the import stops to check storage before anything is published."""
    nd = navidrome_factory()
    w = await world(nd, tmp_path, ("ana", "ben"))
    source = tmp_path / "files"
    mp3 = next(fmt for fmt in IMPORT_FORMATS if fmt.key == "mp3")
    files = [import_tone(source / "mp3", mp3, tagged=False)]
    first = await approve(w, "ana", await upload(w, "ana", files, source, "ana-import"))
    assert first["complete_files"] == 1
    [published] = list((nd.music / "_imports").rglob("*.mp3"))
    data = published.read_bytes()
    before = published.stat().st_ino
    replacement = tmp_path / "replacement.mp3"
    if put == "other bytes":
        data = data[:-1] + bytes([data[-1] ^ 0xFF])
    replacement.write_bytes(data)
    if put == "a link":
        published.unlink()
        published.symlink_to(replacement)
    else:
        replacement.replace(published)
        assert published.stat().st_ino != before and published.stat().st_size == len(data)
    if put == "the same bytes":
        review = await upload(w, "ben", files, source, "ben-import")
        [item] = review["items"]
        assert item["outcome"] == "linked" and item["match"]["reason"] == SAME_BYTES
        assert item["match"]["song_id"] == first["items"][0]["song_id"]
        done = await approve(w, "ben", review)
        assert done["linked_files"] == 1 and done["complete_files"] == 0, done
        assert list((nd.music / "_imports").rglob("*.mp3")) == [published]
    else:
        ben = w.users["ben"][0]
        job = await w.imports.create(ben, "ben-import")
        entry = ManifestEntry("f0", str(files[0].relative_to(source)), files[0].stat().st_size)
        job = await w.imports.declare(ben, job["id"], "page", [entry])
        await w.imports.seal(ben, job["id"])

        async def stream() -> Any:
            yield files[0].read_bytes()

        await w.imports.receive(ben, job["id"], job["items"][0]["id"], 0, stream())
        await w.imports.review(ben, job["id"])
        for _ in range(50):
            status = await w.imports.status(ben, job["id"])
            if status["state"] == "paused":
                break
            assert await w.imports.tick()
        assert (status["state"], status["error"]) == ("paused", "identity"), status
        assert len(list((nd.music / "_imports").rglob("*.mp3"))) == 1
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_concurrent_partial_imports_of_one_album_never_make_two(navidrome_factory, tmp_path):
    nd = navidrome_factory()
    w = await world(nd, tmp_path, ("ana", "ben"))
    album = Album("Glass Orchard", "Paper Garden", LANTERNS[:4], folder="Paper Garden")
    first = write_album(
        tmp_path / "ana", replace(album, tracks=LANTERNS[:2], release_date="2020-01-01")
    )
    second = write_album(
        tmp_path / "ben", replace(album, tracks=LANTERNS[2:4], release_date="2020-06-01")
    )
    review_a = await upload(w, "ana", first, tmp_path / "ana", "ana-import")
    review_b = await upload(w, "ben", second, tmp_path / "ben", "ben-import")
    assert {i["outcome"] for i in review_a["items"] + review_b["items"]} == {"new"}
    await w.imports.approve(w.users["ana"][0], review_a["id"], review_a["revision"], "go-a")
    await w.imports.approve(w.users["ben"][0], review_b["id"], review_b["revision"], "go-b")
    await settle(w, "ana", review_a["id"], "done")
    # Ben's plan would now make a second "Paper Garden": back to review.
    again = await settle(w, "ben", review_b["id"], "review")
    assert again["revision"] > review_b["revision"]
    assert not await approved(w, review_b["id"])
    assert {i["outcome"] for i in again["items"]} == {"copy"}
    assert all("The library changed" in " ".join(i["warnings"]) for i in again["items"])
    await approve(w, "ben", again)
    songs = await album_songs(w, "ben", "Paper Garden")
    assert sorted(s["title"] for s in songs) == [f"Lantern {n}" for n in range(1, 5)]
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_an_import_approved_before_overlap_checks_is_compared_again(
    navidrome_factory, tmp_path
):
    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    source = tmp_path / "ben"
    files = write_album(source, replace(LIBRARY, tracks=LANTERNS[:2], fmt="mp3", folder="S"))
    review = await upload(w, "ben", files, source, "ben-import")
    # Approved files without facts or a decision.
    await w.store.execute(
        "UPDATE music_import_items SET state='eligible',facts='',outcome='',match=''"
    )
    await w.store.execute(
        "UPDATE music_import_jobs SET state='paused',resume_state='publishing',"
        "approved_revision=revision,approval_key='old',error='interrupted'"
    )
    await w.imports.resume(ben, review["id"])
    again = await settle(w, "ben", review["id"], "review")
    assert not await approved(w, review["id"])
    assert {i["outcome"] for i in again["items"]} == {"linked"}
    done = await approve(w, "ben", again)
    assert done["linked_files"] == 2 and done["complete_files"] == 0
    assert len(await album_songs(w, "ben")) == 4
    # Reviewed, not approved, without facts: approval checks it first.
    files = write_album(source, replace(LIBRARY, tracks=LANTERNS[2:3], fmt="mp3", folder="T"))
    review = await upload(w, "ben", files, source, "ben-second")
    await w.store.execute(
        "UPDATE music_import_items SET state='eligible',facts='',outcome='',match=''"
        " WHERE job_id=?",
        [review["id"]],
    )
    with pytest.raises(ImportError_):
        await w.imports.approve(ben, review["id"], review["revision"], "go")
    again = await settle(w, "ben", review["id"], "review")
    assert [i["outcome"] for i in again["items"]] == ["linked"]
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_several_imports_linked_to_one_song_keep_its_credit(navidrome_factory, tmp_path):
    from shijhon.collection.contracts import Query

    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    lantern = (await album_songs(w, "ben"))[0]["id"]
    jobs = []
    for fmt in ("mp3", "m4a"):
        source = tmp_path / fmt
        files = write_album(source, replace(LIBRARY, tracks=LANTERNS[:1], fmt=fmt, folder="S"))
        jobs.append(await approve(w, "ben", await upload(w, "ben", files, source, fmt)))

    async def mine() -> set[str]:
        return set(await w.collection.visible_song_ids(ben, "1", [lantern], mode="original"))

    assert await mine() == {lantern}
    # The Original imported library lists it too, with the rest of its album (albums there
    # are whole).
    listed = await w.collection.browse(
        ben, "1", Query(view="saved", scope="song", pool="personal"), profile_id="original"
    )
    assert sorted(r["title"] for r in listed["rows"]) == [f"Lantern {n}" for n in range(1, 5)]

    async def remove(job: dict[str, Any]) -> None:
        await w.removals.review(ben, job["id"])
        status = await advance(w.removals, ben, job["id"], "review")
        await w.removals.approve(ben, job["id"], status["fingerprint"], "remove-" + job["id"])
        await advance(w.removals, ben, job["id"], "removed")

    await remove(jobs[0])
    assert await mine() == {lantern}  # the other import still gives it
    await remove(jobs[1])
    assert await mine() == set()
    await w.removals.restore(ben, jobs[0]["id"])
    await advance(w.removals, ben, jobs[0]["id"], "restored")
    assert await mine() == {lantern}
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()
