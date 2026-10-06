"""Suite I (continued) - the fill policy and one release filling one album,
against a real Navidrome with the demo catalog's records replayed.

Automatic fills (albums a search, an artist page or a view shows, the library pass) happen
only when the owner has at least 3 songs of the album or a quarter of it; the other albums
are matched and kept as ``deferred``: a view shows them complete at once and writes
nothing, and the first use of the album or one of its catalog songs - a play's report, a
star, a rating of the album - fills it, without asking the catalog again. Catalog IDs a
client kept from the view keep working after the fill, in any method. The library
pass looks at every album (it plans those below the policy).

Owned files of one release can form several albums (for example: one song tagged
"Album", five tagged "Album (Remastered)", one release "Album (Remastered)" in the
catalog). The release fills the album with the closest title and most owned songs,
whatever order they are opened in; the other album's song plays for its track there, and
that album goes to review as its part. Log lines name the album and its owned songs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.fill.fills import ChoiceRefused
from tests.acceptance.test_J_contract import check
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.logs import collected
from tests.harness.replay import Replay

# The one catalog release: "Northern Letters (Remastered)", 17 tracks (demo:900000021).
PART = Album(
    "Neve Ashdown",
    "Northern Letters",
    (Track("Copper Station", 0, seconds=243),),  # no track number, as some real files have none
    recording_date="1962",
)
WHOLE = Album(
    "Neve Ashdown",
    "Northern Letters (Remastered)",
    (
        Track("Crimson Mirrors", 1, seconds=259),
        Track("Open", 2, seconds=181),
        Track("Faded Wolves", 3, seconds=208),
        Track("Open Mirrors", 4, seconds=207),
        Track("Pale Islands", 0, seconds=170),
    ),
    recording_date="1962",
)
# One song of a 19-track soundtrack: below the policy.
FEW = Album(
    "Uma Okafor",
    "Bright Wolves 2 (Original Motion Picture Soundtrack)",
    (Track("Northern Machines", 11, seconds=84),),
    recording_date="2012",
)
# Three songs of a 14-track album: enough.
ENOUGH = Album(
    "Esme Ashdown",
    "CRIMSON.",
    (
        Track("NORTHERN.", 1, seconds=118),
        Track("AMBER.", 2, seconds=186),
        Track("AMBER. 2", 3, seconds=160),
    ),
    recording_date="2010",
)
# Unknown to the catalog: one song (not looked at by the pass), and one song of a
# four-track release by its files' track total (a quarter: looked at).
LONE = Album("Lone Owner", "Lone Song Album", (Track("Only", 1),))
QUARTER = Album("Quarter Owner", "Quarter Album", (Track("First", 1),), track_total=4)
# One song of a 13-track album each: below the policy.
SALT = Album("BrokenMeadow", "Salt 2", (Track("Burning Engines", 1, seconds=301),),
             recording_date="1984")  # fmt: skip
IRON = Album("Oren Garrow", "Iron Wolves", (Track("Distant Towers", 1, seconds=274),),
             recording_date="2006")  # fmt: skip
# One song of a 9-track EP, and one of a 14-track album (found by its ISRC): below the policy.
STONES = Album("Jonah Fairbanks", "wild stones", (Track("FROZEN", 1, seconds=194),),
               recording_date="2010")  # fmt: skip
RIVERS = Album("Esme Ashdown", "PALE RIVERS.",
               (Track("NORTHERN.", 14, seconds=118, isrc="ZZ-SHJ-00-00059"),),
               recording_date="2010")  # fmt: skip


def replay_for(editions: int = 1) -> Replay:
    """``editions``: 1, the remaster alone in the catalog; 2, the standard
    "Northern Letters" too."""
    replay = Replay()
    for title in ("northern letters", "northern letters (remastered)"):
        replay.aliases[f"neve ashdown {title}"] = "tender rivers"
    replay.hidden = {"900000001", "900000042"} if editions == 1 else {"900000042"}
    replay.aliases["uma okafor"] = "restless voices"
    replay.aliases[f"uma okafor {FEW.title.lower()}"] = "restless voices"
    replay.aliases["esme ashdown"] = "bright canyon"
    replay.aliases["esme ashdown crimson."] = "bright canyon"
    for key in ("brokenmeadow", "brokenmeadow salt 2"):
        replay.aliases[key] = "restless islands"
    for key in ("oren garrow", "oren garrow iron wolves"):
        replay.aliases[key] = "crimson towers"
    for key in ("jonah fairbanks", "jonah fairbanks wild stones"):
        replay.aliases[key] = "northern horizon"
    replay.aliases["esme ashdown pale rivers."] = "bright canyon"  # misses: found by ISRC
    return replay


@contextmanager
def world_with(
    factory: NavidromeFactory,
    tmp: Path,
    albums: list[Album],
    replay: Replay,
    **settings: Any,
) -> Iterator[DeliveryWorld]:
    nd = factory()
    for album in albums:
        write_album(nd.music, album)
    nd.scan(full=True)
    fill = {"enabled": True, "open_budget_seconds": 15, "background_pause_seconds": 0,
            "library_pass": "off", **settings}  # fmt: skip
    with delivery_world(nd, tmp, catalog=replay.catalog(), fill=fill) as world:
        yield world


def album_id(world: DeliveryWorld, title: str) -> str:
    found = world.nd.client().ok("search3", {"query": title, "artistCount": 0, "songCount": 0})
    [ident] = [a["id"] for a in found["searchResult3"]["album"] if a["name"] == title]
    return str(ident)


def row(world: DeliveryWorld, ident: str) -> dict[str, Any] | None:
    async def read() -> dict[str, Any] | None:
        found = await world.services.store.fetchone(
            "SELECT outcome, release_ref, reason, owned_songs, release_tracks, plan"
            " FROM album_matches WHERE album_id = ?",
            [ident],
        )
        return dict(found) if found else None

    return world.server.call(read)


def songs(world: DeliveryWorld, ident: str) -> int:
    return int(world.nd.client().ok("getAlbum", {"id": ident})["album"]["songCount"])


def wait_for(world: DeliveryWorld, ident: str, outcome: str) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = row(world, ident)
        if found is not None and found["outcome"] == outcome:
            return found
        time.sleep(0.1)
    raise AssertionError(f"{ident}: {row(world, ident)}")


def open_album(world: DeliveryWorld, ident: str, client: str) -> None:
    world.client(client=client).ok("getAlbum", {"id": ident})


def check_one_album(world: DeliveryWorld, part: str, whole: str, lines: list[str]) -> None:
    """The release filled the "(Remastered)" album; the one-song album is its part."""
    assert songs(world, whole) == 17 and songs(world, part) == 1
    filled = wait_for(world, whole, "filled")
    assert filled["release_ref"] == "demo:900000021"
    # The policy's count: the part's song is owned too.
    assert (filled["owned_songs"], filled["release_tracks"]) == (6, 17)
    reviewed = wait_for(world, part, "review")
    assert reviewed["reason"].startswith(
        f"part of Neve Ashdown - Northern Letters (Remastered) (album {whole})"
    )
    # The part's song plays for its track in the filled album.
    owned = world.nd.client().ok("getAlbum", {"id": part})["album"]["song"][0]["id"]

    async def backing() -> tuple[str, str | None]:
        found = await world.services.store.fetchone(
            "SELECT song_id, backing_song_id FROM placeholders WHERE title = 'Copper Station'"
        )
        assert found is not None
        return str(found["song_id"]), found["backing_song_id"]

    placeholder, backed_by = world.server.call(backing)
    assert backed_by == owned
    played = world.client().request("stream", {"id": placeholder}).content
    assert played == world.nd.client().request("stream", {"id": owned}).content
    assert any(
        line.startswith("filled Neve Ashdown - Northern Letters (Remastered) (5 owned + 1 from"
                        " another album, of 17) with 12 placeholder(s), 1 playing an owned song"
                        " of another album")
        for line in lines
    )  # fmt: skip
    assert any(
        line.startswith("album match needs review: Neve Ashdown - Northern Letters (1 owned):"
                        " part of Neve Ashdown - Northern Letters (Remastered)")
        for line in lines
    )  # fmt: skip


@pytest.mark.parametrize("editions", [1, 2])
@pytest.mark.parametrize("first", ["part", "whole"])
def test_one_release_fills_one_album_whatever_the_order(
    navidrome_factory: NavidromeFactory, tmp_path: Path, first: str, editions: int
) -> None:
    replay = replay_for(editions)
    with world_with(navidrome_factory, tmp_path, [PART, WHOLE], replay) as world:
        part, whole = album_id(world, PART.title), album_id(world, WHOLE.title)
        order = [part, whole] if first == "part" else [whole, part]
        with collected("shijhon.fill.fills") as lines:
            for n, ident in enumerate(order):  # views; the policy allows the fill
                open_album(world, ident, f"viewer-{n}")
            wait_for(world, whole, "filled")
        check_one_album(world, part, whole, lines)
        before = replay.api_requests
        open_album(world, part, "opener-again")  # a part is not matched again
        assert replay.api_requests == before and songs(world, part) == 1


@pytest.fixture(scope="module")
def policy_world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, Replay]]:
    replay = replay_for()
    albums = [FEW, ENOUGH, LONE, QUARTER, SALT, IRON, STONES, RIVERS]
    with world_with(navidrome_factory, tmp_path_factory.mktemp("policy"), albums, replay) as w:
        yield w, replay


def test_a_shown_album_owned_too_little_is_shown_complete_and_filled_on_first_use(
    policy_world: tuple[DeliveryWorld, Replay],
) -> None:
    world, replay = policy_world
    few = album_id(world, FEW.title)
    world.client(client="searcher").ok("search3", {"query": "uma okafor"})  # shows it
    deferred = wait_for(world, few, "deferred")
    assert (deferred["owned_songs"], deferred["release_tracks"]) == (1, 19)
    assert deferred["reason"] == "1 owned, of 19: shown complete, filled on first use"
    assert deferred["plan"] and songs(world, few) == 1  # not filled automatically
    before = replay.api_requests
    viewer = world.client(client="viewer")
    album = viewer.ok("getAlbum", {"id": few})["album"]
    assert album["songCount"] == 19 and len(album["song"]) == 19  # complete at once
    assert check("song", album["song"]) == (1, 18)  # decode like Navidrome's own songs
    check("album", [album])
    assert songs(world, few) == 1 and replay.api_requests == before  # nothing written or asked
    track = next(s for s in album["song"] if s["id"].startswith("sh.tr."))
    with collected("shijhon.fill.fills") as lines:
        viewer.ok("star", {"id": track["id"]})  # the first use
    # Filled from the plan: the only request looks up which release the song is on.
    assert songs(world, few) == 19 and replay.api_requests - before <= 1
    assert (row(world, few) or {}).get("outcome") == "filled"
    assert any(line.endswith("(first use: star)") for line in lines)
    starred = viewer.ok("getStarred2")["starred2"]["song"]
    assert [s["title"] for s in starred] == [track["title"]]
    assert not starred[0]["id"].startswith("sh.")  # on the placeholder


def test_a_catalog_song_s_report_fills_its_album_and_its_id_keeps_working(
    policy_world: tuple[DeliveryWorld, Replay],
) -> None:
    """The catalog album ID opens the owned album; "now playing" of one of the catalog
    songs fills it; the catalog song ID then reaches the placeholder in any method."""
    world, _ = policy_world
    salt = album_id(world, SALT.title)
    viewer = world.client(client="reporter")
    assert viewer.ok("getAlbum", {"id": salt})["album"]["songCount"] == 13  # matched now
    album = viewer.ok("getAlbum", {"id": "sh.al.demo.900000203"})["album"]
    assert album["id"] == salt and album["songCount"] == 13
    assert songs(world, salt) == 1
    track = next(s for s in album["song"] if s["track"] == 5)
    with collected("shijhon.fill.fills") as lines:
        viewer.ok("scrobble", {"id": track["id"], "submission": "false"})
    assert songs(world, salt) == 13
    assert any(line.endswith("(first use: scrobble)") for line in lines)
    native = viewer.ok("getAlbum", {"id": salt})["album"]
    placeholder = next(s["id"] for s in native["song"] if s["track"] == 5)
    playing = viewer.ok("getNowPlaying")["nowPlaying"]["entry"]
    assert [e["id"] for e in playing] == [placeholder]
    for method in ("getSong", "getLyricsBySongId", "getSimilarSongs2"):
        by_catalog = viewer.request(method, {"id": track["id"]}).json()["subsonic-response"]
        by_native = viewer.request(method, {"id": placeholder}).json()["subsonic-response"]
        assert by_catalog == by_native, method


def test_a_rating_of_the_album_itself_fills_it(
    policy_world: tuple[DeliveryWorld, Replay],
) -> None:
    world, _ = policy_world
    iron = album_id(world, IRON.title)
    rater = world.client(client="rater")
    assert rater.ok("getAlbum", {"id": iron})["album"]["songCount"] == 13
    assert songs(world, iron) == 1
    with collected("shijhon.fill.fills") as lines:
        rater.ok("setRating", {"id": iron, "rating": "4"})
    assert songs(world, iron) == 13
    assert any(line.endswith("(first use: setRating)") for line in lines)
    assert rater.ok("getAlbum", {"id": iron})["album"]["userRating"] == 4


def test_a_shown_album_owned_enough_is_filled(
    policy_world: tuple[DeliveryWorld, Replay],
) -> None:
    world, _ = policy_world
    enough = album_id(world, ENOUGH.title)
    world.client(client="browser").ok("search3", {"query": "esme ashdown"})  # shows it
    filled = wait_for(world, enough, "filled")
    assert (filled["owned_songs"], filled["release_tracks"]) == (3, 14)
    assert songs(world, enough) == 14


def test_a_pending_album_s_catalog_ids_show_it_and_the_queue_fills_it_during_a_rest(
    policy_world: tuple[DeliveryWorld, Replay],
) -> None:
    """Before the fill, the release's catalog IDs show the owned album (song, info, cover);
    the saved queue's current song fills it, also while the catalog rests (the plan needs
    no catalog request)."""
    world, _ = policy_world
    stones = album_id(world, STONES.title)
    viewer = world.client(client="queuer")
    album = viewer.ok("getAlbum", {"id": stones})["album"]
    assert album["songCount"] == 9 and songs(world, stones) == 1
    track = next(s for s in album["song"] if s["track"] == 3)
    song = viewer.ok("getSong", {"id": track["id"]})["song"]
    for key in ("albumId", "parent", "album", "coverArt", "year", "albumArtists"):
        assert song[key] == track[key], key
    info = viewer.ok("getAlbumInfo2", {"id": "sh.al.demo.900000133"})
    assert info == viewer.ok("getAlbumInfo2", {"id": stones})
    cover = viewer.request("getCoverArt", {"id": "al-sh.al.demo.900000133"}).content
    assert cover == viewer.request("getCoverArt", {"id": f"al-{stones}"}).content
    fills = world.services.fills
    assert fills is not None
    fills.resting_until = fills.clock() + 60  # a catalog failure a moment ago
    try:
        owned = next(s["id"] for s in album["song"] if not s["id"].startswith("sh."))
        ids = [s["id"] for s in album["song"]]
        viewer.ok("savePlayQueue", {"id": ids, "current": track["id"]})
    finally:
        fills.resting_until = 0.0
    assert songs(world, stones) == 9
    queue = viewer.ok("getPlayQueue")["playQueue"]
    assert len(queue["entry"]) == 9 and queue["entry"][0]["id"] == owned  # none left out


def test_a_failed_first_use_never_becomes_a_catalog_copy(
    policy_world: tuple[DeliveryWorld, Replay],
) -> None:
    world, _ = policy_world
    rivers = album_id(world, RIVERS.title)
    viewer = world.client(client="failing")
    album = viewer.ok("getAlbum", {"id": rivers})["album"]
    assert album["songCount"] == 14
    track = next(s for s in album["song"] if s["id"].startswith("sh.tr."))
    engine = world.services.engine
    engine.tag_hook = lambda comments: comments.__setitem__("releasedate", ["1901"])  # a split
    try:
        first = viewer.request("star", {"id": track["id"]}).json()["subsonic-response"]
        again = viewer.request("star", {"id": track["id"]}).json()["subsonic-response"]
    finally:
        engine.tag_hook = None
    assert first["status"] == again["status"] == "failed"
    assert songs(world, rivers) == 1
    assert (row(world, rivers) or {}).get("outcome") == "failed"

    async def releases() -> list[str]:
        rows = await world.services.store.fetchall("SELECT ref FROM releases")
        return [str(r["ref"]) for r in rows]

    assert "demo:900000101" not in world.server.call(releases)  # no second copy
    starred = viewer.ok("getStarred2")["starred2"].get("song", [])
    assert track["title"] not in [s["title"] for s in starred]


def test_catalog_ids_in_other_methods(policy_world: tuple[DeliveryWorld, Replay]) -> None:
    """A form POST body counts; an ID of a release shown with an owned album is that
    album's; an ID not in the library outside the read-only methods gets Navidrome's own
    answer."""
    world, _ = policy_world
    few = album_id(world, FEW.title)
    client = world.client(client="other-methods")
    client.ok("getAlbum", {"id": few})  # matched (deferred): shown with the owned album
    # Navidrome answers an album's similar songs with a random sample of its library.
    ours = client.ok("getSimilarSongs", {"id": "sh.al.demo.900000182"})["similarSongs"]
    assert ours.get("song") and not any(s["id"].startswith("sh.") for s in ours["song"])
    posted = client.request("getLyricsBySongId", {"id": "sh.tr.demo.999"}, http_method="POST")
    assert posted.json()["subsonic-response"]["lyricsList"] == {}
    navidrome = world.nd.client()
    assert client.error_code("getPlaylist", {"id": "sh.tr.demo.999"}) == navidrome.error_code(
        "getPlaylist", {"id": "sh.tr.demo.999"}
    )


def test_the_library_pass_plans_every_album(
    policy_world: tuple[DeliveryWorld, Replay],
) -> None:
    """Also those below the policy (one song, no track total): matched and kept with their
    plan, so a view shows them complete at once, even among many."""
    world, replay = policy_world
    library_pass = world.services.library_pass
    assert library_pass is not None
    library_pass.mode = "dry_run"
    try:
        world.server.call(library_pass.run_once)
    finally:
        library_pass.mode = "off"
    assert (row(world, album_id(world, LONE.title)) or {}).get("outcome") == "none"
    assert any("lone" in line.lower() for line in replay.log)
    assert (row(world, album_id(world, QUARTER.title)) or {}).get("outcome") == "none"


def test_before_its_fill_a_part_s_track_plays_the_owned_song_and_the_album_id_fills_it(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    """The remaster shown complete and not filled (a policy that fills nothing
    automatically): its track owned in the one-song album plays that owned song; a star of
    the release's catalog album ID fills the owned album - never a catalog copy."""
    replay = replay_for()
    never = {"auto_min_songs": 100, "auto_min_share": 1.0}
    with world_with(navidrome_factory, tmp_path, [PART, WHOLE], replay, **never) as world:
        part, whole = album_id(world, PART.title), album_id(world, WHOLE.title)
        viewer = world.client(client="viewer")
        album = viewer.ok("getAlbum", {"id": whole})["album"]
        assert album["songCount"] == 17 and songs(world, whole) == 5
        copper = next(s for s in album["song"] if s["title"] == "Copper Station")
        assert copper["id"].startswith("sh.tr.")
        owned = world.nd.client().ok("getAlbum", {"id": part})["album"]["song"][0]["id"]
        played = viewer.request("stream", {"id": copper["id"]}).content
        assert played == world.nd.client().request("stream", {"id": owned}).content
        assert songs(world, whole) == 5  # a plain play fills nothing
        viewer.ok("star", {"albumId": "sh.al.demo.900000021"})
        assert songs(world, whole) == 17
        assert [a["id"] for a in viewer.ok("getStarred2")["starred2"]["album"]] == [whole]
        assert songs(world, part) == 1


def test_a_part_matched_again_stays_a_part_of_its_filled_album(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    """The review list's "Match again" on a part whose album is filled: its songs play
    there, so it stays "part of" - never filled from another edition of that release, which
    would put the same songs in two albums; choosing a release for it is refused too."""
    replay = replay_for(2)
    with world_with(navidrome_factory, tmp_path, [PART, WHOLE], replay) as world:
        part, whole = album_id(world, PART.title), album_id(world, WHOLE.title)
        for n, ident in enumerate((whole, part)):
            open_album(world, ident, f"viewer-{n}")
        wait_for(world, whole, "filled")
        fills = world.services.fills
        assert fills is not None

        async def match_again() -> None:  # as the dashboard's action does
            assert fills is not None
            await fills.rematch(part)
            await fills.fill(part, hold=True)

        world.server.call(match_again)
        again = row(world, part) or {}
        assert again.get("outcome") == "review"
        assert again["reason"].startswith(
            f"part of Neve Ashdown - Northern Letters (Remastered) (album {whole})"
        )

        async def choose() -> None:
            assert fills is not None
            await fills.choose(part, "demo:900000001")

        with pytest.raises(ChoiceRefused, match="its songs are part of Neve Ashdown"):
            world.server.call(choose)
        assert songs(world, part) == 1


def test_one_release_is_filled_once_whatever_fills_it_at_the_same_moment(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    """Two owned albums a release completes, chosen for it at the same moment (the review
    list's fills of two rows, or one with the pass): one fills, the other is refused - the
    release in the library as another album - never both."""
    first = Album("BrokenMeadow", "Salt Two", (Track("Burning Engines", 1, seconds=301),))
    second = Album("BrokenMeadow", "Salt Too", (Track("Wild Cities", 2, seconds=256),))
    with world_with(navidrome_factory, tmp_path, [first, second], replay_for()) as world:
        idents = [album_id(world, first.title), album_id(world, second.title)]
        fills = world.services.fills
        assert fills is not None
        outcomes: list[str] = []

        async def both() -> None:
            assert fills is not None

            async def one(ident: str) -> None:
                try:
                    decision = await fills.choose(ident, "demo:900000203")
                    outcomes.append(decision.outcome)
                except ChoiceRefused as exc:
                    outcomes.append(exc.reason)

            async with anyio.create_task_group() as tg:
                for ident in idents:
                    tg.start_soon(one, ident)

        world.server.call(both)
        assert sorted(outcomes) == ["filled", "the release is in the library as another album"]
        assert sorted(songs(world, ident) for ident in idents) == [1, 13]
