"""Exact-release first use and negative refresh against disposable stock Navidrome."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest
from mutagen.flac import FLAC
from pydantic import SecretStr

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.fill.fills import JOINED, Decision, Fills
from shijhon.matching.matcher import confirm, owned_fingerprint
from shijhon.placeholders.engine import MaterializeError
from shijhon.playlists.model import Connection, Definition, PlaylistError, Track
from shijhon.playlists.service import release_fingerprint
from shijhon.views.commits import CommitError
from shijhon.views.ids import CatalogId
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, tone, write_album
from tests.harness.library import Track as OwnedTrack
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay


def release_for(case: str) -> CatalogRelease:
    ref = CatalogRef("demo", f"exact{case}")
    names = ("Arrival", "Lanterns (with Guest)", "Crossing", "Afterglow", "Return")
    return CatalogRelease(
        ref,
        f"Night Letters {case}",
        f"Invented Harbor {case}",
        ReleaseKind.ALBUM,
        "2020",
        tracks=tuple(
            CatalogTrack(
                CatalogRef("demo", f"exact{case}song{n}"),
                title,
                f"Invented Harbor {case}",
                3000,
                number=n,
                album=ref,
                album_title=f"Night Letters {case}",
            )
            for n, title in enumerate(names, 1)
        ),
    )


# More albums of the artist than the old lookup read (100).
CROWDED = ("crowded", "crowdedfits", "commonword", "twocompat", "guested", "spelled")
FILLERS = 105


def filler(music: Path, artist: str, title: str, singer: str, folder: str) -> None:
    """A one-song album of another title (one cached tone, tagged here)."""
    path = music / folder / "1-01 Filler.flac"
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tone(440, 1.0, "flac"), path)
    tags = FLAC(path)
    tags.update(
        {"title": "Filler", "artist": singer, "albumartist": artist, "album": title}
        | {"tracknumber": "1", "date": "2011"}
        | ({"compilation": "1"} if artist == "Various Artists" else {})
    )
    tags.save()


def picture(path: Path, source: str) -> bytes:
    """An invented picture (an ffmpeg test pattern) at ``path``."""
    command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", source]
    subprocess.run([*command, "-frames:v", "1", str(path)], check=True)
    return path.read_bytes()


# Another album of the name with a cover (and, but for "samecover", another one in the catalog).
COVERED = ("collisioncover", "samecover", "spelledother")


@pytest.fixture
def setup(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[DeliveryWorld, CatalogRelease, str, list[Path]]]:
    case = getattr(request, "param", "default")
    release = release_for(case)
    if case == "selftitled":  # the band's later album of the same name
        release = replace(
            release,
            title=release.artist,
            tracks=tuple(replace(t, album_title=release.artist) for t in release.tracks),
        )
    if case == "curlytitle":  # a title with an apostrophe
        title = f"Owner's Letters {case}"
        release = replace(
            release,
            title=title,
            tracks=tuple(replace(t, album_title=title) for t in release.tracks),
        )
    if case == "dashartist":  # an artist with a hyphen
        artist = f"Tide-Paper {case}"
        release = replace(
            release, artist=artist, tracks=tuple(replace(t, artist=artist) for t in release.tracks)
        )
    if case in CROWDED and case not in ("guested", "spelled"):
        artist = "Harbor" if case == "commonword" else "Various Artists"
        release = replace(
            release,
            artist=artist,
            kind=ReleaseKind.ALBUM if case == "commonword" else ReleaseKind.COMPILATION,
            tracks=tuple(replace(t, artist=f"Invented Singer {t.number}") for t in release.tracks),
        )
    if case == "isrc":  # the release and the owned files know their ISRCs
        release = replace(
            release,
            tracks=tuple(replace(t, isrc=f"ZZSHJ200000{t.number}") for t in release.tracks),
        )
    owned_tracks = (
        release.tracks
        if case == "allowned"
        else (release.tracks[:2] if case in ("totals", "larger") else release.tracks[1::2])
    )
    album = Album(
        release.artist,
        release.title,
        tuple(
            OwnedTrack(
                t.title.replace(" (with Guest)", ""),
                t.number,
                artists=(t.artist,),
                isrc=t.isrc or (f"ZZSHJ300000{t.number}" if case == "isrcswap" else None),
            )
            for t in owned_tracks
        ),
        track_total=2 if case == "totals" else None,
        disc_total=1 if case == "totals" else None,
        recording_date="2020",
    )
    if case == "spelledother":  # another album of the title, the release's date tag
        album = replace(
            album,
            tracks=(OwnedTrack("Earlier Song", 1), OwnedTrack("Earlier Hymn", 2)),
            release_date=release.release_date,
        )
    if case in COVERED:  # other songs, the release's date tag: Navidrome would group them
        album = replace(
            album,
            tracks=(OwnedTrack("Earlier Song", 1), OwnedTrack("Earlier Hymn", 2)),
            release_date=release.release_date,
        )
    if case == "selftitled":  # the band's first album: other songs, the same title
        album = replace(
            album,
            tracks=(OwnedTrack("Earlier Song", 1, seconds=3), OwnedTrack("Earlier Hymn", 2)),
            recording_date="1994",
        )
    if case in ("deluxe", "deluxedated"):  # an owned deluxe edition with a bonus song
        album = replace(
            album,
            title=album.title + " (Deluxe Edition)",
            tracks=(*album.tracks, OwnedTrack("Hidden Bonus", 6, seconds=3)),
        )
    if case == "deluxedated":  # ... with the release date tag of the standard release
        album = replace(album, release_date=release.release_date)
    if case in ("collision", "curlytitle", "dashartist"):  # Navidrome groups the release in
        album = replace(album, release_date=release.release_date)
    if case == "curlytitle":  # ... its album tag with a curly apostrophe (str.Clear: the same)
        album = replace(album, title=album.title.replace("'", "\u2019"))
    if case == "dashartist":  # ... its album artist tag with an em dash (the same)
        album = replace(album, artist=album.artist.replace("-", "\u2014"))
    if case in ("crowded", "commonword"):  # no owned album of the release's title
        album = replace(album, title="Unrelated Collection")
    if case in ("crowdedfits", "twocompat"):  # its title written without a space
        album = replace(album, title=album.title.replace("Night Letters", "NightLetters"))
    nd = navidrome_factory()
    paths = write_album(nd.music, album)
    if case in COVERED:  # the owned album's cover: a test pattern beside its songs
        mine = picture(paths[0].parent / "cover.jpg", "testsrc=size=320x320")
    for n in range(FILLERS if case in CROWDED else 0):
        if case == "spelled":  # albums of the release's title by other artists
            filler(nd.music, f"Other Singer {n:03d}", release.title, "x", f"f/{n}")
        elif case == "commonword":  # matched by the artist's name, a common word
            filler(nd.music, f"Invented Crowd {n:03d}", f"Harbor Lights {n:03d}", "x", f"f/{n}")
        else:  # listed before the release's title
            filler(nd.music, release.artist, f"Aa Filler {n:03d}", f"Singer {n}", f"f/{n}")
    if case in ("ambiguous", "twocompat"):  # a second compatible album: an edition
        second = album.title.replace("NightLetters", "Night Letters") + " (Remastered)"
        paths += write_album(nd.music, replace(album, title=second, folder="other-edition"))
    if case in ("spelled", "spelledother"):  # the catalog writes the artist otherwise
        release = replace(
            release, artist=release.artist.replace("Invented Harbor", "InventedHarbor")
        )
    if case == "guested":  # the album credited with a guest: "A feat. B" (its songs: A)
        release = replace(release, artist=f"{release.artist} feat. Invented Guest")
    if case == "otherartist":
        release = replace(
            release,
            artist="Another Invented Artist",
            tracks=tuple(replace(t, artist="Another Invented Artist") for t in release.tracks),
        )
    if case == "incomplete":
        release = replace(release, incomplete=True)
    if case in ("bad", "collision", "curlytitle", "dashartist", "keptjoin"):
        release = replace(
            release,
            tracks=tuple(
                replace(t, title="Different Recording") if t.number == 2 else t
                for t in release.tracks
            ),
        )
    if case == "numbering":
        release = replace(
            release,
            tracks=tuple(replace(t, number=2) if t.number == 1 else t for t in release.tracks),
        )
    if case == "unnumbered":
        release = replace(
            release,
            numbered=False,
            tracks=tuple(replace(t, number=6) if t.number == 2 else t for t in release.tracks),
        )
    if case == "larger":
        release = replace(release, title=release.title + " (Deluxe)")
    nd.scan(full=True)
    listed = nd.client().ok("getAlbumList2", {"type": "alphabeticalByName", "size": "500"})
    album_id = next(a["id"] for a in listed["albumList2"]["album"] if a["name"] == album.title)
    catalog = Replay().catalog()
    if case in COVERED:  # the catalog's cover: the same picture, or another one
        release = replace(release, artwork_template="https://covers.demo.invalid/x/{w}x{h}.png")
        theirs = picture(nd.root / "theirs.png", "mandelbrot=size=320x320")
        catalog.replay.artwork_answer = (mine if case == "samecover" else theirs, "image/png")

    async def search(term: str, limit: int = 20) -> SearchResults:
        raise AssertionError("exact-release commit must not rerun the broad matcher")

    async def album_by_id(ident: str) -> CatalogRelease:
        assert ident == release.ref.id
        return release

    async def song(ident: str) -> CatalogTrack:
        return next(t for t in release.tracks if t.ref.id == ident)

    catalog.search, catalog.album, catalog.song = search, album_by_id, song  # type: ignore[method-assign]
    try:
        with delivery_world(
            nd,
            tmp_path,
            catalog=catalog,
            fill={"library_pass": "dry_run", "auto_min_songs": 99999, "auto_min_share": 1.0},
        ) as world:
            fills = world.services.fills
            assert fills

            async def prepare() -> None:
                # Deterministic tests: no view/exposure worker may change the album.
                fills.spawn = None
                owned = await fills._owned(album_id)
                assert owned
                if case != "unmatched":
                    await fills._record(album_id, Decision("none", "old negative"), owned)
                    await world.services.store.execute(
                        "UPDATE album_matches SET match_revision=0, owned_fingerprint=NULL,"
                        " plan=NULL, planned=0 WHERE album_id=?",
                        [album_id],
                    )
                if case in ("kept", "keptjoin"):
                    await fills.keep(album_id)
                if case == "review":
                    await fills._record(album_id, Decision("review", "owner review"), owned)
                if case == "settled":
                    await fills._record(album_id, Decision("complete", "complete match"), owned)

            world.server.call(prepare)
            yield world, release, album_id, paths
    finally:
        nd.stop()


async def connected(world: DeliveryWorld) -> Connection:
    service = world.services.playlists
    assert service
    auth = await service.client.login(ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
    result = Connection(
        user_id=auth["id"],
        username=auth["username"],
        token=SecretStr(auth["subsonicToken"]),
        salt=SecretStr(auth["subsonicSalt"]),
    )
    await service.repo.connect(result)
    return result


def selected(release: CatalogRelease) -> list[Track]:
    return [
        Track(
            id=f"sh.tr.demo.{t.ref.id}",
            artist=t.artist,
            title=t.title,
            album=release.title,
            duration=t.seconds,
            release=str(release.ref),
            unfamiliar=True,
        )
        for t in release.tracks
    ]


def hashes(paths: list[Path]) -> dict[Path, str]:
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


@pytest.mark.parametrize("setup", ["stalenone", "unmatched"], indirect=True)
def test_frozen_full_playlist_uses_exact_release_without_a_plan(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    world, release, album_id, paths = setup
    before = world.nd.client().ok("getAlbum", {"id": album_id})["album"]
    original_files = hashes(paths)
    service, fills = world.services.playlists, world.services.fills
    assert service and fills

    async def publish() -> dict[str, Any]:
        connection = await connected(world)
        # Preview remains read-only even with fresh exact-release evidence.
        prior = await fills._row(album_id)
        prior = dict(prior) if prior else None
        assert await service.matched_edition(selected(release)[0]) == selected(release)[0]
        after = await fills._row(album_id)
        assert (dict(after) if after else None) == prior
        assert not await service.repo.store.fetchall("SELECT * FROM placeholders")
        definition = Definition(count=5, max_per_artist=5, cadence="manual")
        mix = await service.repo.create(connection.user_id, definition)
        run = await service.repo.enqueue(mix)
        await service.repo.update(
            run,
            "publishing",
            {
                "definition": definition.model_dump(),
                "selected": [t.model_dump() for t in selected(release)],
                "releases": [
                    {
                        "ref": str(release.ref),
                        "fingerprint": release_fingerprint(release),
                        "title": release.title,
                        "artist": release.artist,
                        "tracks": 5,
                    }
                ],
                "baseline": "",
                "native_ids": [],
                "intent": "",
                "provisional": "",
                "created_id": "",
                "history_count": 0,
                "history_from": int(time.time()),
                "skipped": 0,
            },
        )
        await service.process(run)
        result = await service.repo.run(run)
        assert result["state"] == "published", result
        saved = await service.repo.mix(mix, connection.user_id)
        native = await service.client.playlist(connection, saved["playlist_id"])
        assert [t["id"] for t in native["entry"]] == result["data"]["native_ids"]
        assert len(native["entry"]) == 5
        assert {t["albumId"] for t in native["entry"]} == {album_id}
        assert result["data"]["selected"] == [t.model_dump() for t in selected(release)]
        return result

    world.server.call(publish)
    assert hashes(paths) == original_files
    after = world.nd.client().ok("getAlbum", {"id": album_id})["album"]
    assert after["songCount"] == 5
    assert {s["id"] for s in before["song"]} <= {s["id"] for s in after["song"]}
    assert world.placeholder_rows() == 3


@pytest.mark.parametrize(
    "setup",
    [
        "incomplete",
        "ambiguous",
        "allowned",
        "kept",
        "review",
        "numbering",
        "unnumbered",
        "larger",
        "collisioncover",
        "keptjoin",
        "twocompat",
    ],
    indirect=True,
)
def test_unsafe_exact_release_never_falls_back_or_changes_owned_decisions(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    """A uniquely compatible owned album is claimed, and a refusal there is final (never a
    standalone album beside it); so is the same album that has a recording the release
    does not, when the owner kept it as it is; several compatible owned albums are refused
    (also an edition titled "NightLetters" among more albums than the old lookup read);
    and when no owned album fits or is the release's album but Navidrome could put the
    release's songs into one (the same album artist, album tag in any letter case and
    release date tag - another album of the name, its cover clearly another), nothing is
    written."""
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    before = world.nd.client().ok("getAlbum", {"id": album_id})["album"]
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits
    row = world.server.call(lambda: fills._row(album_id))

    async def refuse() -> None:
        service = world.services.playlists
        assert service
        connection = await connected(world)
        with pytest.raises(PlaylistError) as caught:
            await service.check_tracks(
                connection,
                selected(release),
                {str(release.ref): release_fingerprint(release)},
            )
        message = str(caught.value)
        assert release.title in message and any(t.artist in message for t in selected(release))
        assert any(t.title in message for t in selected(release))
        assert "Generate again" in message or "generate again" in message or "Library" in message
        if release.ref.id == "exactcollisioncover":
            assert "conflicts with an existing library album" in message
        if release.ref.id in ("exactambiguous", "exacttwocompat"):
            assert "linked to one owned album" in message

    world.server.call(refuse)
    assert not world.placeholder_rows()
    assert commits.materializations == 0
    assert hashes(paths) == original_files
    # The whole answer: Navidrome counts a placeholder written and taken out again as a
    # missing song of the album it joined, so nothing may be written.
    assert world.nd.client().ok("getAlbum", {"id": album_id})["album"] == before
    assert dict(world.server.call(lambda: fills._row(album_id))) == dict(row)


@pytest.mark.parametrize(
    "setup", ["selftitled", "deluxe", "deluxedated", "crowded", "commonword"], indirect=True
)
def test_a_release_no_owned_album_fits_is_added_as_its_own_album(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    """An owned album of the release's title that is not the release's album (the band's
    other album of the same name - no song in common, another year -, an owned deluxe
    edition with a bonus song - also one carrying the standard release's date, a separate
    album to Navidrome by its album tag), and an artist with more albums than one lookup
    reads (a compilation artist, a name that is a common word), do not stop the release:
    it is added as an album of its own, and the owned albums stay as they are."""
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    before = world.nd.client().ok("getAlbum", {"id": album_id})["album"]
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits
    row = world.server.call(lambda: fills._row(album_id))

    async def prepare() -> list[str]:
        service = world.services.playlists
        assert service
        connection = await connected(world)
        approved = {str(release.ref): release_fingerprint(release)}
        return await service.check_tracks(connection, selected(release), approved)

    ids = world.server.call(prepare)
    native = world.nd.client()
    songs = [native.ok("getSong", {"id": ident})["song"] for ident in ids]
    assert [s["title"] for s in songs] == [t.title for t in release.tracks]
    albums = {s["albumId"] for s in songs}
    assert len(albums) == 1 and album_id not in albums
    added = native.ok("getAlbum", {"id": albums.pop()})["album"]
    assert (added["name"], added["songCount"]) == (release.title, 5)
    assert world.placeholder_rows() == 5
    assert commits.materializations == 1 and fills.fills == 0
    assert hashes(paths) == original_files
    assert native.ok("getAlbum", {"id": album_id})["album"] == before
    assert dict(world.server.call(lambda: fills._row(album_id))) == dict(row)


@pytest.mark.parametrize(
    "setup", ["bad", "collision", "curlytitle", "dashartist", "samecover"], indirect=True
)
def test_the_same_album_takes_the_release_songs_it_lacks_never_a_second_album(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    """Never two albums of one name. The owned album has its own
    "Lanterns", the release a "Different Recording" in its place: not every owned recording
    is on the release, but they share "Afterglow" - the same album. The release's songs it
    lacks join it beside its own (both second songs stay), whether Navidrome would have
    grouped a separate album with it (the same date tag, its title or artist written with
    another apostrophe or dash) or not. An album of the name sharing no song, of the same
    year and with the same cover (a second disc of it, say), takes all of them. The owned
    files stay as they were; the artist has one album of that name."""
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    native = world.nd.client()
    before = native.ok("getAlbum", {"id": album_id})["album"]
    owned_ids = {s["id"] for s in before["song"]}
    shared = 0 if release.ref.id == "exactsamecover" else 1
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits

    async def prepare() -> list[str]:
        service = world.services.playlists
        assert service
        connection = await connected(world)
        approved = {str(release.ref): release_fingerprint(release)}
        return await service.check_tracks(connection, selected(release), approved)

    ids = world.server.call(prepare)
    assert {native.ok("getSong", {"id": i})["song"]["albumId"] for i in ids} == {album_id}
    after = native.ok("getAlbum", {"id": album_id})["album"]
    assert after["songCount"] == len(owned_ids) + len(release.tracks) - shared
    assert owned_ids <= {s["id"] for s in after["song"]}
    assert world.placeholder_rows() == len(release.tracks) - shared
    assert fills.fills == 1 and commits.materializations == 0
    assert hashes(paths) == original_files
    artist = native.ok("getArtist", {"id": after["artistId"]})["artist"]
    assert [a["id"] for a in artist["album"] if a["name"] == after["name"]] == [album_id]
    found = native.ok(
        "search3",
        {"query": release.title, "albumCount": "20", "artistCount": "0", "songCount": "0"},
    )["searchResult3"].get("album", [])
    assert [a["id"] for a in found if a["name"] == after["name"]] == [album_id]
    row = world.server.call(lambda: fills._row(album_id))
    assert (row["outcome"], row["reason"], row["release_ref"]) == (
        "filled",
        JOINED,
        str(release.ref),
    )


@pytest.mark.parametrize("setup", ["totals", "settled"], indirect=True)
def test_a_complete_album_of_the_name_takes_a_bigger_edition_s_songs(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    """The owned album is complete - its files' track totals say so, or a match found it
    complete - and the catalog's album of the very same name has more songs: their first
    use adds them into the owned album, never a second album of the name and no refusal."""
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    native = world.nd.client()
    before = native.ok("getAlbum", {"id": album_id})["album"]
    owned_ids = {s["id"] for s in before["song"]}
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits

    async def prepare() -> list[str]:
        service = world.services.playlists
        assert service
        connection = await connected(world)
        approved = {str(release.ref): release_fingerprint(release)}
        return await service.check_tracks(connection, selected(release), approved)

    ids = world.server.call(prepare)
    assert {native.ok("getSong", {"id": i})["song"]["albumId"] for i in ids} == {album_id}
    after = native.ok("getAlbum", {"id": album_id})["album"]
    assert after["songCount"] == len(release.tracks)
    assert owned_ids <= {s["id"] for s in after["song"]}
    assert world.placeholder_rows() == len(release.tracks) - len(owned_ids)
    assert fills.fills == 1 and commits.materializations == 0
    assert hashes(paths) == original_files
    artist = native.ok("getArtist", {"id": after["artistId"]})["artist"]
    assert [a["id"] for a in artist["album"] if a["name"] == after["name"]] == [album_id]
    row = world.server.call(lambda: fills._row(album_id))
    assert (row["outcome"], row["reason"]) == ("filled", JOINED)


@pytest.mark.parametrize("setup", ["crowdedfits", "guested", "spelled"], indirect=True)
def test_every_album_of_the_artist_is_read_and_a_compatible_one_filled(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    """The artist's albums and the albums of the release's title are read to their end, in
    one answer each, for each credited name ("A feat. B": A, B): the compatible owned
    album is found after more albums than the old lookup read, also titled "NightLetters",
    credited to A alone, or credited "Invented Harbor" for a catalog "InventedHarbor"."""
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    native = world.nd.client()
    first = native.ok(
        "search3",
        {"query": release.artist, "albumCount": "100", "artistCount": "0", "songCount": "0"},
    )["searchResult3"].get("album", [])
    assert album_id not in {a["id"] for a in first}  # not what the old lookup read
    owned_ids = {s["id"] for s in native.ok("getAlbum", {"id": album_id})["album"]["song"]}
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits
    searches: list[tuple[str, int]] = []
    read = fills._found

    async def found(query: str, count: int) -> list[dict[str, Any]]:
        searches.append((query, count))
        return await read(query, count)

    fills._found = found  # type: ignore[method-assign]

    async def prepare() -> list[str]:
        service = world.services.playlists
        assert service
        connection = await connected(world)
        approved = {str(release.ref): release_fingerprint(release)}
        return await service.check_tracks(connection, selected(release), approved)

    ids = world.server.call(prepare)
    assert {native.ok("getSong", {"id": i})["song"]["albumId"] for i in ids} == {album_id}
    after = native.ok("getAlbum", {"id": album_id})["album"]
    assert after["songCount"] == 5 and owned_ids <= {s["id"] for s in after["song"]}
    assert world.placeholder_rows() == 3
    assert fills.fills == 1 and commits.materializations == 0
    assert hashes(paths) == original_files
    expected = {
        "exactcrowdedfits": ["Various Artists", release.title],
        "exactguested": ["Invented Harbor guested", "Invented Guest", release.title],
        "exactspelled": [release.artist, release.title],
    }[release.ref.id]
    assert [query for query, _ in searches] == expected
    assert searches[0][1] == 20_001  # every album the search finds, in one answer


@pytest.mark.parametrize("setup", ["crowdedfits"], indirect=True)
def test_a_library_change_during_the_lookup_cannot_hide_an_owned_album(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    """An album the artist search lists before the owned "NightLetters" album leaves the
    artist while the lookup reads (retagged and scanned after its first request). Read in
    pages, the next page's offset then skipped the owned album and the release was added
    beside it; one answer per search still holds it, and it is filled."""
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    native = world.nd.client()
    lookup = {"query": release.artist, "albumCount": "1000", "artistCount": "0", "songCount": "0"}
    order = [a for a in native.ok("search3", lookup)["searchResult3"]["album"]]
    position = [a["id"] for a in order].index(album_id)
    assert position > 0 and order[0]["name"].startswith("Aa Filler ")
    leaving = world.nd.music / f"f/{int(order[0]['name'].rsplit(' ', 1)[1])}" / "1-01 Filler.flac"
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits
    fills.lookup_page = position  # (paged: the owned album opened the second page)
    original = fills.navidrome.subsonic
    searched = 0

    async def subsonic(method: str, params: Any = ()) -> dict[str, Any]:
        nonlocal searched
        answer = await original(method, params)
        if method == "search3" and dict(params).get("query") == release.artist:
            searched += 1
            if searched == 1:

                def leave() -> None:
                    tags = FLAC(leaving)
                    tags["albumartist"] = ["Moved Elsewhere"]
                    del tags["compilation"]
                    tags.save()
                    world.nd.scan()

                await anyio.to_thread.run_sync(leave)
        return answer

    fills.navidrome.subsonic = subsonic  # type: ignore[method-assign]
    try:

        async def prepare() -> list[str]:
            service = world.services.playlists
            assert service
            connection = await connected(world)
            approved = {str(release.ref): release_fingerprint(release)}
            return await service.check_tracks(connection, selected(release), approved)

        ids = world.server.call(prepare)
    finally:
        fills.navidrome.subsonic = original  # type: ignore[method-assign]
    assert searched >= 1
    assert {native.ok("getSong", {"id": i})["song"]["albumId"] for i in ids} == {album_id}
    assert world.placeholder_rows() == 3 and commits.materializations == 0
    assert hashes(paths) == original_files


@pytest.mark.parametrize("setup", ["spelledother"], indirect=True)
def test_another_album_of_the_title_by_the_artist_written_otherwise_stays_apart(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    """An owned album of the release's title by "Invented Harbor" (other songs, the
    release's date tag, clearly another cover: another album of the name), the catalog's
    release by "InventedHarbor": the title search finds it; Navidrome keeps the two album
    artist tags apart, so the release is added as its own album and the owned album stays
    as it is."""
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    native = world.nd.client()
    before = native.ok("getAlbum", {"id": album_id})["album"]
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits

    async def prepare() -> list[str]:
        assert [o.id for _, o in await fills._same_title(release)] == [album_id]
        service = world.services.playlists
        assert service
        connection = await connected(world)
        approved = {str(release.ref): release_fingerprint(release)}
        return await service.check_tracks(connection, selected(release), approved)

    ids = world.server.call(prepare)
    albums = {native.ok("getSong", {"id": i})["song"]["albumId"] for i in ids}
    assert len(albums) == 1 and album_id not in albums
    assert world.placeholder_rows() == 5 and commits.materializations == 1
    assert native.ok("getAlbum", {"id": album_id})["album"] == before
    assert hashes(paths) == original_files


@pytest.mark.parametrize("setup", ["otherartist"], indirect=True)
def test_same_title_by_another_artist_is_not_claimed(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    commits = world.services.commits
    assert commits
    mapping = world.server.call(lambda: commits.natives({selected(release)[0].id}))
    song = world.nd.client().ok("getSong", {"id": next(iter(mapping.values()))})["song"]
    assert song["albumId"] != album_id
    assert world.nd.client().ok("getAlbum", {"id": album_id})["album"]["songCount"] == 2
    assert hashes(paths) == original_files


def test_concurrent_first_uses_share_one_verified_owned_fill(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    world, release, album_id, _ = setup
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits

    async def race() -> None:
        mappings = []

        async def one() -> None:
            mappings.append(await commits.natives({t.id for t in selected(release)}))

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(one)
            tasks.start_soon(one)
        assert mappings[0] == mappings[1]
        assert fills.fills == 1 and commits.materializations == 0

    world.server.call(race)
    assert world.placeholder_rows() == 3
    assert world.nd.client().ok("getAlbum", {"id": album_id})["album"]["songCount"] == 5


def test_normal_fill_racing_exact_commit_keeps_one_release_and_album(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits

    async def matchable(term: str, limit: int = 20) -> SearchResults:
        return SearchResults(albums=(release,))

    async def race() -> None:
        fills.matcher.catalog.search = matchable  # type: ignore[method-assign]

        async def fill() -> None:
            await fills.fill(album_id)

        async def commit() -> None:
            await commits.natives({t.id for t in selected(release)})

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(fill)
            tasks.start_soon(commit)
        assert fills.fills == 1 and commits.materializations == 0
        rows = await world.services.store.fetchall("SELECT album_id FROM releases")
        assert [r["album_id"] for r in rows] == [album_id]

    world.server.call(race)
    assert hashes(paths) == original_files
    assert world.placeholder_rows() == 3


def test_native_metadata_retag_refreshes_negative_but_not_fresh_catalog_misses(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    world, release, album_id, paths = setup
    fills = world.services.fills
    assert fills
    native = world.services.navidrome
    assert native
    calls: list[str] = []
    original = native.album
    original_songs = native.songs_of_album

    async def counted(ident: str) -> dict[str, Any]:
        calls.append("album")
        return await original(ident)

    async def counted_songs(ident: str) -> list[dict[str, Any]]:
        calls.append("songs")
        return await original_songs(ident)

    native.album = counted  # type: ignore[method-assign]
    native.songs_of_album = counted_songs  # type: ignore[method-assign]

    async def cache() -> None:
        owned = await fills._owned(album_id)
        assert owned
        await fills._record(album_id, Decision("none", "fresh miss"), owned, planned=True)
        calls.clear()
        assert await fills.viewable(album_id)
        assert calls == []  # before auth, no native work
        assert await fills.view(album_id, ("owner", "client")) == (None, None)
        assert calls == ["album", "songs"]  # exactly one owned-metadata snapshot
        assert not await fills.due(album_id, dry_run=True)

    world.server.call(cache)
    tags = FLAC(paths[0])
    tags["artist"] = [release.artist + " feat. Invented Guest"]
    tags.save()
    world.nd.scan(full=True)
    assert world.server.call(lambda: fills.due(album_id, dry_run=True))


def test_changed_approval_never_writes_or_substitutes_another_release(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    fills = world.services.fills
    assert fills
    row = world.server.call(lambda: fills._row(album_id))

    async def refuse() -> None:
        service = world.services.playlists
        assert service
        connection = await connected(world)
        with pytest.raises(PlaylistError, match="Generate again"):
            await service.check_tracks(connection, selected(release), {str(release.ref): "changed"})
        assert not await service.repo.store.fetchall("SELECT * FROM releases")

    world.server.call(refuse)
    assert not world.placeholder_rows()
    assert hashes(paths) == original_files
    # The playlist's own approval refused: no fill of the album failed (no wait for others).
    assert dict(world.server.call(lambda: fills._row(album_id))) == dict(row)


def test_frozen_release_cannot_be_replaced_by_another_saved_edition(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    world, release, album_id, paths = setup
    original_files = hashes(paths)

    async def refuse() -> None:
        service, fills = world.services.playlists, world.services.fills
        assert service and fills
        owned = await fills._owned(album_id)
        assert owned
        alternate = replace(release, ref=CatalogRef("demo", "differentedition"))
        links = confirm(owned, alternate)
        assert links
        await fills._record(
            album_id,
            Decision("deferred", "saved edition", alternate, links, release_ref=str(alternate.ref)),
            owned,
        )
        connection = await connected(world)
        with pytest.raises(PlaylistError, match="saved match"):
            await service.check_tracks(
                connection, selected(release), {str(release.ref): release_fingerprint(release)}
            )
        assert not await service.repo.store.fetchall("SELECT * FROM releases")

    world.server.call(refuse)
    assert not world.placeholder_rows()
    assert hashes(paths) == original_files


def test_saved_plan_with_changed_artwork_still_uses_the_exact_recordings(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    world, release, album_id, paths = setup
    original_files = hashes(paths)

    async def fill() -> None:
        fills, commits = world.services.fills, world.services.commits
        assert fills and commits
        owned = await fills._owned(album_id)
        assert owned
        earlier = replace(
            release,
            artwork_template="https://artwork.example/old.jpg",
            tracks=tuple(
                replace(t, artwork_template="https://artwork.example/old.jpg")
                for t in release.tracks
            ),
        )
        links = confirm(owned, earlier)
        assert links
        await fills._record(
            album_id,
            Decision("deferred", "saved match", earlier, links, release_ref=str(earlier.ref)),
            owned,
        )
        mapping = await commits.natives({t.id for t in selected(release)})
        assert len(mapping) == 5 and fills.fills == 1 and commits.materializations == 0

    world.server.call(fill)
    assert world.placeholder_rows() == 3
    assert hashes(paths) == original_files


def test_negative_recheck_builds_a_plan_without_filling_or_scheduling(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    world, release, album_id, paths = setup
    original_files = hashes(paths)
    fills = world.services.fills
    assert fills

    async def matchable(term: str, limit: int = 20) -> SearchResults:
        return SearchResults(albums=(release,))

    async def check() -> None:
        fills.matcher.catalog.search = matchable  # type: ignore[method-assign]
        decision = await fills.recheck(album_id)
        assert decision and decision.outcome == "deferred" and decision.release == release
        assert len(decision.links) == 2
        assert fills._queue == [] and fills._running == {}
        assert (await fills._row(album_id))["outcome"] == "deferred"
        assert await fills.recheck(album_id) is None  # cannot erase the positive plan

    world.server.call(check)
    assert not world.placeholder_rows()
    assert hashes(paths) == original_files


@pytest.mark.parametrize("case", ["album", "song", "failed", "guard", "retagged", "unlisted"])
def test_saved_owned_plan_first_use_survives_catalog_outage_with_its_guards(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
    case: str,
) -> None:
    world, release, album_id, paths = setup
    original_ids = {
        s["id"] for s in world.nd.client().ok("getAlbum", {"id": album_id})["album"]["song"]
    }
    original_files = hashes(paths)
    fills, commits, catalog = world.services.fills, world.services.commits, world.services.catalog
    assert fills and commits and catalog
    album_calls = []

    async def prepare() -> Fills:
        owned = await fills._owned(album_id)
        assert owned
        links = confirm(owned, release)
        assert links
        await fills._record(
            album_id,
            Decision("deferred", "saved match", release, links, release_ref=str(release.ref)),
            owned,
        )
        if case == "failed":
            await fills._record(album_id, Decision("failed", "previous fill failed"), owned)
            await world.services.store.execute(
                "UPDATE album_matches SET checked_at=0 WHERE album_id=?", [album_id]
            )
        # No in-memory match/cache is needed after restart: reconstruct the coordinator
        # over the persisted row and make every catalog album lookup fail.
        restarted = Fills(
            fills.store,
            fills.navidrome,
            fills.engine,
            fills.matcher,
            scope=fills.scope,
            auto_fill=False,
        )
        commits.owned = restarted

        async def unavailable(ident: str) -> CatalogRelease:
            album_calls.append(ident)
            raise CatalogError("unavailable", "invented-private-upstream-error")

        catalog.album = unavailable  # type: ignore[method-assign]
        return restarted

    restarted = world.server.call(prepare)
    if case == "retagged":
        tags = FLAC(paths[0])
        tags["artist"] = [release.artist + " feat. Guest"]
        tags.save()
        world.nd.scan(full=True)
        original_files = hashes(paths)

    async def use() -> None:
        before = dict(await restarted._row(album_id))
        if case == "guard":

            def reject(actual: CatalogRelease) -> None:
                assert actual == release
                raise MaterializeError("the release differs from the saved selection")

            with (
                commits.engine.guard_materializations(reject),
                pytest.raises(CommitError) as caught,
            ):
                await commits.native(CatalogId("al", release.ref), "star")
            assert caught.value.category == "selection_changed"
            assert dict(await restarted._row(album_id)) == before  # not a failed fill
        elif case == "unlisted":
            stray = replace(release.tracks[0], ref=CatalogRef("demo", "unlistedtrack"))

            async def song(ident: str) -> CatalogTrack:
                assert ident == stray.ref.id
                return stray

            catalog.song = song  # type: ignore[method-assign]
            with pytest.raises(CommitError) as caught:
                await commits.native(CatalogId("tr", stray.ref), "star")
            assert caught.value.code == 70
            assert dict(await restarted._row(album_id)) == before
        elif case == "retagged":
            with pytest.raises(CommitError, match="catalog unavailable"):
                await commits.native(CatalogId("al", release.ref), "star")
            assert album_calls == [release.ref.id]
            assert dict(await restarted._row(album_id)) == before
        elif case == "song":
            await commits.native(CatalogId("tr", release.tracks[0].ref), "star")
        else:
            await commits.native(CatalogId("al", release.ref), "star")
        if case != "retagged":
            assert album_calls == []

    world.server.call(use)
    assert hashes(paths) == original_files
    after = world.nd.client().ok("getAlbum", {"id": album_id})["album"]
    assert original_ids <= {s["id"] for s in after["song"]}
    if case in ("album", "song", "failed"):
        assert world.placeholder_rows() == 3 and after["songCount"] == 5
        assert restarted.fills == 1 and commits.materializations == 0
    else:
        assert not world.placeholder_rows() and after["songCount"] == 2


@pytest.mark.parametrize("retag", ["year", "isrc"])
def test_a_retag_that_keeps_an_album_shown_complete_keeps_it_fillable(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]], retag: str
) -> None:
    """Views and the library pass compare the owned songs as the saved plan saw them: a
    new year or ISRC keeps the album shown complete, and using one of the release's
    catalog songs fills it."""
    world, release, album_id, paths = setup
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits

    async def plan() -> None:
        owned = await fills._owned(album_id)
        assert owned
        links = confirm(owned, release)
        assert links
        saved = Decision("deferred", "saved match", release, links, release_ref=str(release.ref))
        await fills._record(album_id, saved, owned)

    world.server.call(plan)
    original_ids = {
        s["id"] for s in world.nd.client().ok("getAlbum", {"id": album_id})["album"]["song"]
    }
    for path in paths if retag == "year" else paths[:1]:
        tags = FLAC(path)
        tags.update({"date": "2021"} if retag == "year" else {"isrc": "ZZSHJ2100001"})
        tags.save()
    world.nd.scan(full=True)
    original_files = hashes(paths)

    async def use() -> None:
        owned = await fills._owned(album_id)
        row = await fills._row(album_id)
        assert owned and row["owned_fingerprint"] != owned_fingerprint(owned)  # retagged
        assert await fills._shown(album_id) is not None  # still shown complete
        await commits.native(CatalogId("tr", release.tracks[0].ref), "star")

    world.server.call(use)
    after = world.nd.client().ok("getAlbum", {"id": album_id})["album"]
    assert after["songCount"] == 5 and original_ids <= {s["id"] for s in after["song"]}
    assert world.placeholder_rows() == 3 and fills.fills == 1
    assert hashes(paths) == original_files


@pytest.mark.parametrize("setup", ["isrc", "isrcswap"], indirect=True)
def test_a_retag_to_another_recordings_isrc_is_not_filled_from_its_plan(
    setup: tuple[DeliveryWorld, CatalogRelease, str, list[Path]],
) -> None:
    """An owned song's known ISRC replaced by another: not the recording the saved plan
    linked, whether the catalog's track has an ISRC or none. The album is no longer shown
    complete from that plan, and the release's first use refuses as it did before (no
    placeholders)."""
    world, release, album_id, paths = setup
    fills, commits = world.services.fills, world.services.commits
    assert fills and commits

    async def plan() -> None:
        owned = await fills._owned(album_id)
        assert owned and all(song.isrcs for song in owned.songs)
        links = confirm(owned, release)
        assert links
        saved = Decision("deferred", "saved match", release, links, release_ref=str(release.ref))
        await fills._record(album_id, saved, owned)
        assert await fills._shown(album_id) is not None

    world.server.call(plan)
    tags = FLAC(paths[0])
    tags["isrc"] = ["ZZSHJ2999999"]
    tags.save()
    world.nd.scan(full=True)
    original_files = hashes(paths)
    before = world.nd.client().ok("getAlbum", {"id": album_id})["album"]

    async def use() -> None:
        assert await fills._shown(album_id) is None  # views and the pass: match it again
        with pytest.raises(CommitError) as caught:
            await commits.native(CatalogId("tr", release.tracks[0].ref), "star")
        assert caught.value.category == "owned_plan"

    world.server.call(use)
    assert not world.placeholder_rows() and fills.fills == 0
    assert world.nd.client().ok("getAlbum", {"id": album_id})["album"] == before
    assert hashes(paths) == original_files
