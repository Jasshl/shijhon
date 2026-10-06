"""Suite L — owned-recording backing.

A catalog release that contains a recording the owner already has on another release
plays the local file's bytes for that track; add-ons are not asked. User actions stay on
the placeholder — including "now playing": Navidrome 0.64.2 sets it only from
``scrobble(submission=false)`` and ``reportPlayback``, which carry the placeholder's ID.

The owned recordings are found when the placeholders are created (the same ISRC, or the
same recording title, artist and length without a clean/explicit contradiction), and a
catalog song not in the library yet plays the owned file too.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

import pytest

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import CatalogRelease, CatalogTrack
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import PLACEHOLDER_FOLDER, catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_USER
from tests.harness.subsonic import SubsonicClient


@dataclass
class Backed:
    world: DeliveryWorld
    addon: FakeAddon
    placeholder: str
    owned: str


@pytest.fixture(scope="module")
def backed(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Backed]:
    nd = navidrome_factory({"ND_ENABLESHARING": "true"})
    single = Album("Backing Band", "The Hit - Single", (Track("The Hit", 1, seconds=4),))
    write_album(nd.music, single)
    nd.scan(full=True)
    owned = nd.client().ok("search3", {"query": "The Hit"})["searchResult3"]["song"][0]["id"]
    with delivery_world(nd, tmp_path_factory.mktemp("l"), budget_seconds=8.0) as world:
        addon = world.addon("Source")
        world.add_source(addon)
        release = catalog_release("l-album", "The Album", "Backing Band", 3)
        result = world.materialize(release)
        placeholder = result.created[release.tracks[1].ref]
        world.server.call(lambda: world.services.engine.set_backing(placeholder, owned))
        yield Backed(world, addon, placeholder, owned)


def test_plays_the_owned_file(backed: Backed) -> None:
    client = backed.world.client()
    via_placeholder = client.request("stream", {"id": backed.placeholder})
    direct = backed.world.nd.client().request("stream", {"id": backed.owned})
    assert via_placeholder.content == direct.content
    assert via_placeholder.content[:4] == b"fLaC"
    part = client.request("stream", {"id": backed.placeholder}, headers={"range": "bytes=10-99"})
    assert part.status_code == 206 and part.content == direct.content[10:100]
    download = client.request("download", {"id": backed.placeholder})
    assert download.content == direct.content
    transcoded = client.request(
        "stream", {"id": backed.placeholder, "format": "mp3", "maxBitRate": 96}
    )
    assert transcoded.headers["content-type"] == "audio/mpeg"
    assert backed.addon.requests() == []


def test_user_actions_stay_on_the_placeholder(backed: Backed) -> None:
    client = backed.world.client()
    client.ok("star", {"id": backed.placeholder})
    starred = {s["id"] for s in client.ok("getStarred2")["starred2"]["song"]}
    assert backed.placeholder in starred and backed.owned not in starred
    client.ok("scrobble", {"id": backed.placeholder, "submission": "true"})
    assert client.ok("getSong", {"id": backed.placeholder})["song"].get("playCount") == 1
    assert client.ok("getSong", {"id": backed.owned})["song"].get("playCount") in (None, 0)


def test_now_playing_is_the_placeholder(backed: Backed) -> None:
    client = backed.world.client()
    client.request("stream", {"id": backed.placeholder})
    assert client.ok("getNowPlaying")["nowPlaying"].get("entry", []) == []  # streams set nothing
    client.ok("scrobble", {"id": backed.placeholder, "submission": "false"})
    playing = {e["id"] for e in client.ok("getNowPlaying")["nowPlaying"].get("entry", [])}
    assert playing == {backed.placeholder}


def test_wrong_credentials_get_no_work_for_a_backed_placeholder(backed: Backed) -> None:
    """Credentials first: the owned song's presence check (a service-account lookup,
    possibly a write) runs only for a verified caller."""
    world = backed.world
    recordings = world.services.interceptor.recordings
    assert recordings is not None
    forget_presence(world)
    looked: list[str] = []
    present = recordings.present

    async def spy(song_id: str) -> str:
        looked.append(song_id)
        return await present(song_id)

    recordings.present = spy  # type: ignore[method-assign]
    stranger = SubsonicClient(world.server.base_url, ADMIN_USER, "not-the-password")
    try:
        for method in ("stream", "download"):  # Navidrome's own credential error
            assert stranger.error_code(method, {"id": backed.placeholder}) == 40
        assert looked == []
        world.client().request("stream", {"id": backed.placeholder})
        assert looked == [backed.owned]  # the owned song it plays
    finally:
        recordings.__dict__.pop("present", None)
        stranger.close()


def test_share_link_gets_a_copy_of_the_owned_file(backed: Backed) -> None:
    import json
    import re

    import httpx

    world = backed.world
    share = world.client().ok("createShare", {"id": backed.placeholder})["shares"]["share"][0]
    page = httpx.get(world.server.base_url + "/share/" + share["id"])
    info = re.search(r"__SHARE_INFO__\s*=\s*(\".*?\")\s*</script>", page.text, re.S)
    assert info is not None
    token = json.loads(json.loads(info.group(1)))["tracks"][0]["id"]
    audio = httpx.get(f"{world.server.base_url}/share/s/{token}")
    assert audio.content[:4] == b"fLaC" and len(audio.content) > 10_000
    assert backed.addon.requests() == []


# --- found by matching across releases -------------------------------------------------

ISRC_DEEP = "ZZSHJ9900002"
OWNED_ELSEWHERE = [
    # The same recording title, artist and length, no ISRC: the single.
    Album("Cross Band", "Cross Hit - Single", (Track("Cross Hit", 1, seconds=4),)),
    # The same ISRC under a remaster's title.
    Album(
        "Cross Band",
        "Deep Cuts",
        (Track("Deep Cut - 2011 Remaster", 1, seconds=5, isrc=ISRC_DEEP),),
    ),
    Album("Cross Band", "Live Set", (Track("Encore (Live)", 1, seconds=6),)),  # another take
    # A clean edit (advisory 2) of an explicit track.
    Album(
        "Cross Band",
        "Clean Hits",
        (Track("Rude Song", 1, seconds=7),),
        extra={"ITUNESADVISORY": ["2"]},
    ),
    Album("Cross Band", "Long Versions", (Track("Long Song", 1, seconds=14),)),  # other length
    # Two known ISRCs that differ: two recordings.
    Album("Cross Band", "Codes", (Track("Twin Code", 1, seconds=4, isrc="ZZSHJ9909999"),)),
    # A title another album of the artist has too: not the same recording by its title.
    Album("Cross Band", "Other Record", (Track("Intro", 1, seconds=5),)),
    # An edition of the release itself (its remaster): the same recording by its title.
    Album("Cross Band", "Cross Album (Remastered)", (Track("Love Letter", 3, seconds=6),)),
    # The original of a re-recording ("(... Version)" naming another take).
    Album("Cross Band", "Tv Song - Single", (Track("Tv Song", 1, seconds=4),)),
]


def cross_release(key: str) -> CatalogRelease:
    base = catalog_release(key, "Cross Album", "Cross Band", 10)
    titles = [("Cross Hit", 4), ("Deep Cut", 5), ("Encore", 6), ("Rude Song", 7),
              ("Long Song", 8), ("Twin Code", 4), ("Nowhere Else", 5), ("Intro", 5),
              ("Love Letter", 6), ("Tv Song (Cross Band's Version)", 4)]  # fmt: skip
    tracks = tuple(
        replace(
            t,
            title=title,
            duration_ms=seconds * 1000,
            explicit=title == "Rude Song",
            isrc=ISRC_DEEP if title == "Deep Cut" else t.isrc,
        )
        for t, (title, seconds) in zip(base.tracks, titles, strict=True)
    )
    return replace(base, tracks=tracks)


class OneRelease:
    """A catalog holding one release (plays of its songs before any commit)."""

    key = "test"
    region = "xx"

    def __init__(self, release: CatalogRelease) -> None:
        self.release = release

    async def song(self, song_id: str) -> CatalogTrack:
        for track in self.release.tracks:
            if track.ref.id == song_id:
                return track
        raise CatalogError("not_found", "test")

    async def album(self, album_id: str) -> CatalogRelease:
        if album_id != self.release.ref.id:
            raise CatalogError("not_found", "test")
        return self.release

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        return SearchResults()

    async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
        return ()

    async def artist(self, artist_id: str) -> Any:
        raise CatalogError("not_found", "test")

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        return ()

    async def artwork(self, url: str) -> tuple[bytes, str]:
        raise CatalogError("not_found", "test")

    async def aclose(self) -> None:
        return None


@dataclass
class Cross:
    world: DeliveryWorld
    addon: FakeAddon
    owned: dict[str, str]  # owned song title -> song ID


@pytest.fixture(scope="module")
def cross(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Cross]:
    nd = navidrome_factory()
    for album in OWNED_ELSEWHERE:
        write_album(nd.music, album)
    nd.scan(full=True)
    songs = nd.client().ok("search3", {"query": "", "songCount": 50})["searchResult3"]["song"]
    owned = {s["title"]: s["id"] for s in songs}
    catalog = OneRelease(cross_release("l-cross-play"))
    tmp = tmp_path_factory.mktemp("l-cross")
    with delivery_world(nd, tmp, budget_seconds=8.0, catalog=catalog) as world:  # type: ignore[arg-type]
        addon = world.addon("Source")
        world.add_source(addon)
        yield Cross(world, addon, owned)


def test_new_placeholders_of_owned_recordings_are_backed(cross: Cross) -> None:
    cross.addon.clear()
    release = cross_release("l-cross")
    result = cross.world.materialize(release)
    placeholders = {t.title: result.created[t.ref] for t in release.tracks}

    async def backing() -> dict[str, str | None]:
        rows = await cross.world.services.store.fetchall(
            "SELECT title, backing_song_id FROM placeholders WHERE release_ref = ?",
            [str(release.ref)],
        )
        return {r["title"]: r["backing_song_id"] for r in rows}

    assert cross.world.server.call(backing) == {
        "Cross Hit": cross.owned["Cross Hit"],  # title, artist and length
        "Deep Cut": cross.owned["Deep Cut - 2011 Remaster"],  # the same ISRC
        "Encore": None,  # "(Live)" is another recording
        "Rude Song": None,  # the owned file is the clean edit
        "Long Song": None,  # another length
        "Twin Code": None,  # another ISRC
        "Nowhere Else": None,
        "Intro": None,  # the same title on another album
        "Love Letter": cross.owned["Love Letter"],  # an edition of this album
        "Tv Song (Cross Band's Version)": None,  # a re-recording
    }
    client = cross.world.client()
    played = client.request("stream", {"id": placeholders["Cross Hit"]})
    direct = cross.world.nd.client().request("stream", {"id": cross.owned["Cross Hit"]})
    assert played.content == direct.content and played.content[:4] == b"fLaC"
    assert cross.addon.requests() == []


def test_a_catalog_song_not_in_the_library_plays_the_owned_file(cross: Cross) -> None:
    """Before any commit: the owned file through Navidrome, not the add-ons, also at another
    bitrate (no placeholder needed); nothing is committed."""
    cross.addon.clear()
    track = next(t for t in cross_release("l-cross-play").tracks if t.title == "Cross Hit")
    ident = f"sh.tr.test.{track.ref.id}"
    client = cross.world.client()
    direct = cross.world.nd.client().request("stream", {"id": cross.owned["Cross Hit"]})
    assert client.request("stream", {"id": ident}).content == direct.content
    lower = client.request("stream", {"id": ident, "maxBitRate": 96, "format": "mp3"})
    assert lower.headers["content-type"] == "audio/mpeg"
    assert cross.addon.requests() == []

    async def committed() -> int:
        row = await cross.world.services.store.fetchone(
            "SELECT COUNT(*) AS n FROM placeholders WHERE release_ref = 'test:l-cross-play'"
        )
        return int(row["n"]) if row else -1

    assert cross.world.server.call(committed) == 0  # nothing committed


def test_a_backing_whose_owned_file_is_missing_falls_back_to_the_add_ons(cross: Cross) -> None:
    world = cross.world
    single = Album("Stale Band", "Stale Song - Single", (Track("Stale Song", 1, seconds=4),))
    write_album(world.nd.music, single)
    world.nd.scan(targets=[single.relative_folder])
    base = catalog_release("l-stale", "Stale Album", "Stale Band", 1)
    release = replace(base, tracks=(replace(base.tracks[0], title="Stale Song", duration_ms=4000),))
    placeholder = world.materialize(release).created[release.tracks[0].ref]

    async def backing() -> str | None:
        row = await world.services.store.fetchone(
            "SELECT backing_song_id FROM placeholders WHERE song_id = ?", [placeholder]
        )
        return row["backing_song_id"] if row else "?"

    assert world.server.call(backing) is not None
    audio = world.audio("l-stale")
    track = release.tracks[0]
    assert track.isrc is not None
    cross.addon.add(FakeTrack(isrc=track.isrc, audio=audio))
    for path in (world.nd.music / single.relative_folder).iterdir():
        path.unlink()
    world.nd.scan(targets=[single.relative_folder])
    forget_presence(world)  # as after a while: checked again
    played = world.client().request("stream", {"id": placeholder})
    assert played.content == audio.read_bytes()  # from the add-on
    # Navidrome keeps the missing song (it may come back: a disk away): so does the backing.
    assert world.server.call(backing) is not None


def forget_presence(world: DeliveryWorld) -> None:
    """Forget which owned songs are known to be there (checked again after a while)."""

    async def forget() -> None:
        recordings = world.services.interceptor.recordings
        assert recordings is not None
        recordings._present.clear()

    world.server.call(forget)


def test_a_file_in_the_placeholder_folder_is_never_an_owned_recording(cross: Cross) -> None:
    """A play of a catalog song while its release is being committed would find the
    release's new placeholder - scanned by Navidrome, not recorded by Shijhon yet - by its
    ISRC, and play it (silence). A file in the placeholder folder is never an owned recording,
    recorded or not: the add-ons play the song."""
    world = cross.world
    track = next(t for t in cross_release("l-cross-play").tracks if t.title == "Encore")
    assert track.isrc is not None
    recordings = world.services.interceptor.recordings
    assert recordings is not None
    recordings._found.clear()  # looked up afresh
    unrecorded = Album(
        "Cross Band",
        "Cross Album",
        (Track("Encore", 3, seconds=6, isrc=track.isrc),),
        folder=f"{PLACEHOLDER_FOLDER}/Cross Band/Cross Album [committing]",
    )
    write_album(world.nd.music, unrecorded)
    world.nd.scan(targets=[unrecorded.relative_folder])
    audio = world.audio("l-encore", seconds=6)
    cross.addon.add(FakeTrack(isrc=track.isrc, audio=audio))
    try:
        answer = world.client().request("stream", {"id": f"sh.tr.test.{track.ref.id}"})
        assert answer.content == audio.read_bytes()  # the add-on's audio, not that file
    finally:
        shutil.rmtree(world.nd.music / unrecorded.relative_folder)
        world.nd.scan(targets=[unrecorded.relative_folder])
