"""Suite I (continued) - another release for a filled album, against a real Navidrome.

An album filled from the wrong edition is filled from another release the owner chooses on
the Library page: the songs the fill added are taken out as the cleanup takes a release
out - only when nobody has used any of them, by Navidrome's own records; no record of it
is kept - and the album is filled from the chosen release through the ordinary fill. A fill
anyone has used stays as it is, whole, and so does every album while nothing says who uses
what. (What a stop in the middle leaves: suite R.)

The page's flow runs with the demo catalog's records replayed (the automatic picks' world:
albums the catalog has in two editions); the rules of the choice with releases of the
engine's own harness.
"""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shijhon.cleanup import NO_USAGE
from shijhon.fill.fills import IN_LIBRARY, ChoiceRefused, InUse
from shijhon.navidrome.usage import UsageSource
from shijhon.placeholders.engine import MaterializeError, Removal, Statements
from tests.acceptance.test_I_auto_pick import (
    EDITIONS as CRIMSON,
)
from tests.acceptance.test_I_auto_pick import (
    IRON,
    NEVER,
    SALT,
    STONES,
    WOLVES,
    album_id,
    replay_for_picks,
    row,
    songs,
    view,
    wait_for,
)
from tests.acceptance.test_M_cleanup import fill_songs
from tests.conftest import NavidromeFactory
from tests.harness.collection import prepare_collection
from tests.harness.dashboard import PATH, Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import (
    EDITIONS_ARTIST,
    EngineParts,
    cleanup_for,
    edition,
    engine_for,
    fills_for,
)
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance

PICKED = [WOLVES, STONES, SALT, IRON, CRIMSON]
BEFORE: dict[str, Path] = {}  # a copy of Navidrome's database from before the fills
# Each album's two editions here: the one picked, and its copy under another ID.
EDITIONS = {
    WOLVES.title: ("demo:900000182", "demo:900009182"),
    STONES.title: ("demo:900000133", "demo:900009133"),
    SALT.title: ("demo:900000203", "demo:900009203"),
    IRON.title: ("demo:900000296", "demo:900009296"),
    CRIMSON.title: ("demo:900000117", "demo:900009117"),
}


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    for album in PICKED:
        write_album(nd.music, album)
    nd.scan(full=True)
    fill = {"enabled": True, "open_budget_seconds": 15, "background_pause_seconds": 0,
            "library_pass": "off", "pass_requests_per_second": 50, **NEVER}  # fmt: skip
    tmp = tmp_path_factory.mktemp("change-release")
    # Navidrome's records as they are before anything is filled: not the library's
    # current ones once a fill is in it.
    shutil.copy(nd.data / "navidrome.db", tmp / "before.db")
    with delivery_world(
        nd,
        tmp,
        catalog=replay_for_picks().catalog(),
        fill=fill,
        navidrome_database=nd.data / "navidrome.db",  # (the cleanup itself stays off)
    ) as w:
        w.app.dashboard.attempts.limit = 10_000
        prepare_collection(nd, w.server)
        for album in PICKED:  # viewed: picked, shown complete
            view(w, album_id(w, album.title), f"viewer-{album.title}")
        for album in PICKED:
            wait_for(w, album_id(w, album.title), "deferred")
        BEFORE[w.server.base_url] = tmp / "before.db"
        yield w


@pytest.fixture
def admin(world: DeliveryWorld) -> Iterator[Browser]:
    browser = Browser(world.server.base_url)
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    yield browser
    browser.close()


def filled(world: DeliveryWorld, album: Album) -> tuple[str, list[str]]:
    """The album, filled from its pick without a use of it: its ID and the songs added."""
    ident = album_id(world, album.title)
    fills = world.services.fills
    assert fills is not None
    if filling(world, ident) != [EDITIONS[album.title][0]]:  # (else: as another test left it)
        decision = world.server.call(lambda: fills.fill_album(ident, "a test"))
        assert decision is not None and decision.outcome == "filled", decision
    added = fill_songs(world, ident)
    assert added
    return ident, added


def unused(world: DeliveryWorld, album: Album) -> tuple[str, list[str], str, str]:
    """The album, filled and never used, with whichever of its two editions another test
    left it: its ID, the songs added, the release it is filled from, and the other."""
    ident = album_id(world, album.title)
    if not filling(world, ident):
        fills = world.services.fills
        assert fills is not None
        decision = world.server.call(lambda: fills.fill_album(ident, "a test"))
        assert decision is not None and decision.outcome == "filled", decision
    [current] = filling(world, ident)
    other = next(release for release in EDITIONS[album.title] if release != current)
    return ident, fill_songs(world, ident), current, other


def change(admin: Browser, ident: str, release: str, name: str = "picked") -> Any:
    sent = {"csrf": admin.csrf(), "page": "1", "list": name, "release": release}
    return admin.post(f"library/change/{ident}", sent)


def settle(world: DeliveryWorld, ident: str, seconds: float = 180) -> None:
    deadline = time.monotonic() + seconds
    while world.app.dashboard.jobs.running(ident) is not None:
        assert time.monotonic() < deadline, "the background action did not finish"
        time.sleep(0.2)


def listed_row(page: str, anchor: str, title: str) -> str:
    section = page.split(f'id="{anchor}"', 1)[1].split("</section>", 1)[0]
    found = [r for r in section.split("<tr>") if f'<span class="cell-title">{title}</span>' in r]
    assert found, title
    return found[0]


def filling(world: DeliveryWorld, ident: str) -> list[str]:
    async def read() -> list[str]:
        rows = await world.services.store.fetchall(
            "SELECT ref FROM releases WHERE owned_album_id = ?", [ident]
        )
        return [str(r["ref"]) for r in rows]

    return list(world.server.call(read))


def tracks(world: DeliveryWorld, ident: str) -> set[str]:
    """The catalog tracks the album's added songs stand for."""

    async def read() -> set[str]:
        rows = await world.services.store.fetchall(
            "SELECT p.track_ref FROM placeholders p JOIN releases r ON p.release_ref = r.ref"
            " WHERE r.owned_album_id = ?",
            [ident],
        )
        return {str(r["track_ref"]) for r in rows}

    return set(world.server.call(read))


def removed(world: DeliveryWorld, release: str) -> bool:
    return world.server.call(lambda: world.services.engine.removed_record(release)) is not None


# --- the Library page -------------------------------------------------------------------


def test_a_filled_album_is_filled_from_another_release(
    world: DeliveryWorld, admin: Browser
) -> None:
    first, other = EDITIONS[WOLVES.title]
    ident, added = filled(world, WOLVES)
    assert len(added) == 18 and filling(world, ident) == [first]
    its_tracks = tracks(world, ident)
    # The pick's row offers the other release it listed, folded away.
    listed = listed_row(admin.get("library").text, "picked", WOLVES.title)
    assert "Filled from it." in listed and "Wrong edition?" in listed
    assert f'action="{PATH}/library/change/{ident}"' in listed
    assert re.findall(r'<option value="(demo:\d+)">', listed) == [other]
    assert 'value="keep"' not in listed and 'value="rematch"' not in listed
    answer = change(admin, ident, other)
    assert answer.headers["location"] == f"{PATH}/library?picks=1#picked"
    settle(world, ident)
    page = admin.get("library").text
    assert "the 18 songs its old release added were taken out, 18 added." in page
    found = row(world, ident) or {}
    assert (found["outcome"], found["release_ref"], found["picked"]) == ("filled", other, "")
    assert sorted(found["candidates"].split(",")) == sorted([first, other])
    assert filling(world, ident) == [other]
    # The album has each track once: its own song and the chosen release's others. Here
    # the two editions have every song under the same number and title, and Navidrome
    # gives each new song the ID the old one had.
    assert tracks(world, ident) == {f"{other}{n:02d}" for n in range(1, 20)} - {f"{other}11"}
    assert fill_songs(world, ident) == added
    assert len(songs(world, ident)) == 19
    # No record of the release taken out is kept: its IDs are the new songs' now.
    assert not removed(world, first) and not removed(world, other)
    assert world.services.engine.removed_release(added[0]) is None
    # Chosen by the owner: no longer a pick. It is among the filled albums, which are
    # listed when asked for, with the release it had to go back to.
    page = admin.get("library").text
    assert f">{WOLVES.title}<" not in page.split('id="picked"', 1)[-1].split("</section>")[0]
    assert 'href="?filled=1#filled"' in page and f">{WOLVES.title}<" not in page
    listed = listed_row(admin.get("library?filled=1").text, "filled", WOLVES.title)
    assert re.findall(r'<option value="(demo:\d+)">', listed) == [first]
    # The search narrows the filled albums too, shown or only counted.
    page = admin.get("library?q=okafor+wolves&filled=1").text
    assert "1 album found" in page.split('id="filled"', 1)[1].split("</section>")[0]
    assert listed_row(page, "filled", WOLVES.title)
    assert (
        'href="?q=okafor+wolves&amp;filled=1#filled"' in admin.get("library?q=okafor+wolves").text
    )
    assert "No filled album matches" in admin.get("library?q=no+such+album&filled=1").text
    assert 'id="filled"' not in admin.get("library?q=no+such+album").text
    # Filled from the first release again, its songs are back under the IDs they had.
    answer = change(admin, ident, first, "filled")
    assert answer.headers["location"] == f"{PATH}/library?filled=1#filled"
    settle(world, ident)
    assert filling(world, ident) == [first] and fill_songs(world, ident) == added
    assert tracks(world, ident) == its_tracks
    assert not removed(world, other) and not removed(world, first)


def test_a_change_whose_fill_fails_is_said_and_finished_by_shijhon(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident, added, _, other = unused(world, IRON)
    engine, fills = world.services.engine, world.services.fills
    assert fills is not None
    materialize = engine.materialize_held

    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise MaterializeError("Navidrome did not index the new songs")

    monkeypatch.setattr(engine, "materialize_held", failing)
    change(admin, ident, other)
    settle(world, ident)
    page = admin.get("library").text
    assert "were taken out, but it was not filled from" in page
    assert "(Navidrome did not index the new songs)" in page
    assert "Shijhon tries the fill again by itself, also after a restart." in page
    # The old songs are out, the album is the chosen release's plan - shown complete, and
    # not one the fill policy fills -, and the page keeps saying what is left to do.
    assert filling(world, ident) == [] and fill_songs(world, ident) == []
    found = row(world, ident) or {}
    assert (found["outcome"], found["release_ref"]) == ("deferred", other)
    shown = view(world, ident, "after-change")["song"]
    assert len(shown) == found["release_tracks"] and len(songs(world, ident)) == 1
    assert not world.server.call(lambda: fills.due(ident))
    page = admin.get("library").text
    assert "is not filled from" in page and "yet (Navidrome did not index the new songs)" in page
    assert "Shijhon tries the fill again by itself." in page
    # Its wait over, Shijhon's own retry fills the album.
    monkeypatch.setattr(engine, "materialize_held", materialize)
    replacements = fills.replacements
    monkeypatch.setattr(replacements, "clock", lambda: time.time() + 120)
    assert world.server.call(replacements.retry) == 1
    assert filling(world, ident) == [other] and fill_songs(world, ident) == added
    assert (row(world, ident) or {})["outcome"] == "filled"
    assert "is not filled from" not in admin.get("library").text


def test_a_fill_anyone_has_used_stays_as_it_is(world: DeliveryWorld, admin: Browser) -> None:
    first, other = EDITIONS[STONES.title]
    ident, added = filled(world, STONES)
    before = row(world, ident)
    client = world.client(client="listener")
    client.ok("star", {"id": added[3]})
    change(admin, ident, other)
    settle(world, ident)
    page = admin.get("library").text
    assert "stays as it is: songs its fill added are in use (favorited). Nothing was changed." in (
        page
    )
    assert filling(world, ident) == [first] and fill_songs(world, ident) == added
    assert row(world, ident) == before and not removed(world, first)
    # A use that ended still counts, as for the cleanup.
    client.ok("unstar", {"id": added[3]})
    change(admin, ident, other)
    settle(world, ident)
    assert "songs its fill added are in use" in admin.get("library").text
    assert filling(world, ident) == [first] and fill_songs(world, ident) == added
    for song in added:  # every song is where it was, playable as before
        assert world.nd.client().ok("getSong", {"id": song})["song"]["albumId"] == ident


def test_a_stream_shijhon_served_keeps_the_fill(world: DeliveryWorld, admin: Browser) -> None:
    first, other = EDITIONS[SALT.title]
    ident, added = filled(world, SALT)

    async def served() -> None:
        await world.services.store.execute(
            "UPDATE placeholders SET last_used_at = ? WHERE song_id = ?", [time.time(), added[0]]
        )

    world.server.call(served)
    change(admin, ident, other)
    settle(world, ident)
    assert "are in use (streamed or downloaded). Nothing was changed." in admin.get("library").text
    assert filling(world, ident) == [first] and fill_songs(world, ident) == added


def test_a_use_that_comes_as_the_fill_is_taken_out_keeps_it(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody used the fill when it was listed; a favorite given before its files go is
    seen by the removal's own look under the release's locks."""
    first, other = EDITIONS[CRIMSON.title]
    ident, added = filled(world, CRIMSON)
    cleanup = world.services.cleanup
    assert cleanup is not None
    check = cleanup._check
    client = world.nd.client()  # (Navidrome itself: asked from within Shijhon's own loop)

    def late(*args: Any) -> Any:
        client.ok("star", {"id": added[0]})  # (after the listing, before the removal)
        return check(*args)

    monkeypatch.setattr(cleanup, "_check", late)
    change(admin, ident, other)
    settle(world, ident)
    assert "songs its fill added are in use (favorited)" in admin.get("library").text
    assert filling(world, ident) == [first] and fill_songs(world, ident) == added
    for song in added:
        assert world.nd.client().ok("getSong", {"id": song})["song"]["albumId"] == ident


def test_nothing_is_changed_while_nothing_says_who_uses_what(
    world: DeliveryWorld, admin: Browser
) -> None:
    ident, added, first, other = unused(world, IRON)
    cleanup = world.services.cleanup
    assert cleanup is not None
    usage = cleanup.usage
    try:
        # No records of use at all: as for the cleanup, nothing is taken out.
        cleanup.usage = None
        change(admin, ident, other)
        settle(world, ident)
        assert f"was not changed: {NO_USAGE}." in admin.get("library").text.replace("&#39;", "'")
        assert filling(world, ident) == [first] and fill_songs(world, ident) == added
        # Records that are not this library's current ones (a copy made before the fill).
        cleanup.usage = UsageSource(BEFORE[world.server.base_url], export=False)
        change(admin, ident, other)
        settle(world, ident)
        page = admin.get("library").text
        assert "was not changed: Navidrome's records do not have the songs" in page.replace(
            "&#39;", "'"
        )
        assert filling(world, ident) == [first] and fill_songs(world, ident) == added
    finally:
        cleanup.usage = usage
    # With them, the release nobody used is changed.
    change(admin, ident, other)
    settle(world, ident)
    assert filling(world, ident) == [other] and len(fill_songs(world, ident)) == len(added)


def test_only_another_listed_release_of_a_filled_album_can_be_chosen(
    world: DeliveryWorld, admin: Browser
) -> None:
    first, other = EDITIONS[STONES.title]
    ident = album_id(world, STONES.title)
    if not filling(world, ident):
        filled(world, STONES)
    for release in (first, "demo:900000117", "", "x"):  # the one it has; one not listed
        change(admin, ident, release)
        assert "choose one of the other listed releases" in admin.get("library").text
    assert world.app.dashboard.jobs.running(ident) is None and filling(world, ident) == [first]
    # An album that is not filled has no release to change.
    assert change(admin, "no-such-album", other).status_code == 303
    assert "That album is no longer filled." in admin.get("library").text
    # The session's token is needed, as for every action.
    sent = {"page": "1", "list": "picked", "release": other}
    assert admin.post(f"library/change/{ident}", {**sent, "csrf": "x"}).status_code == 403
    stranger = Browser(world.server.base_url)
    anonymous = stranger.post(f"library/change/{ident}", sent)
    assert anonymous.status_code == 303 and f"{PATH}/sign-in" in anonymous.headers["location"]
    stranger.close()
    assert filling(world, ident) == [first]


# --- the rules of the choice (the engine's own releases) -----------------------------------


@pytest.fixture(scope="module")
def state(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("change-state")


async def owned_album(nd: NavidromeInstance, title: str, count: int = 1) -> str:
    """An owned album with the first ``count`` songs of the editions of ``title``."""
    tracks = tuple(Track(f"{title} Song {n}", n, seconds=3) for n in range(1, count + 1))
    write_album(nd.music, Album(EDITIONS_ARTIST, title, tracks))
    nd.scan(full=True)
    albums = nd.client().ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})
    return next(str(a["id"]) for a in albums["albumList2"]["album"] if a["name"] == title)


async def numbers(parts: EngineParts, ident: str) -> list[int]:
    found = await parts.navidrome.songs_of_album(ident)
    return sorted(int(s["trackNumber"]) for s in found if not s.get("missing"))


async def match(parts: EngineParts, ident: str) -> Any:
    return await parts.store.fetchone(
        "SELECT outcome, release_ref, reason, plan FROM album_matches WHERE album_id = ?", [ident]
    )


@pytest.mark.anyio
async def test_a_release_that_cannot_fill_the_album_is_refused_before_anything_goes(
    navidrome: NavidromeInstance, state: Path
) -> None:
    title = "I Editions Refused"
    ident = await owned_album(navidrome, title)
    old, new = edition("i-ref-a", 3, title), edition("i-ref-b", 4, title)
    # A release without the owned song; one in the library as another album.
    without = edition("i-ref-c", 4, "I Editions Without")
    elsewhere = edition("i-ref-d", 3, title)
    async with engine_for(navidrome, state) as parts:
        fills = fills_for(parts.engine, parts.store, parts.navidrome, old, new, without, elsewhere)
        calls: list[str] = []

        removal = cleanup_for(parts.engine, navidrome, state, fills).take_out_now

        async def take_out(ref: str, also: Statements) -> Removal:
            calls.append(ref)
            return await removal(ref, also)

        with pytest.raises(ChoiceRefused, match="the album is not filled"):
            await fills.change(ident, str(new.ref), take_out)
        await fills.choose(ident, str(old.ref))
        await parts.store.execute(
            "INSERT INTO releases (ref, folder, album_id, title, artist, album_tags, data,"
            " created_at) VALUES (?, 'elsewhere', 'another-album', ?, ?, '{}', '{}', 1)",
            [str(elsewhere.ref), elsewhere.title, elsewhere.artist],
        )
        before = dict(await match(parts, ident))
        for release, why in (
            (str(old.ref), "the album is filled from this release"),
            (str(without.ref), "an owned track is not on this release"),
            ("test:i-ref-none", "the catalog has no such release"),
            ("other:1", "a release of another catalog"),
            ("x", "not a catalog release"),
            (str(elsewhere.ref), IN_LIBRARY),
        ):
            with pytest.raises(ChoiceRefused, match=re.escape(why)):
                await fills.change(ident, release, take_out)
        assert calls == [] and dict(await match(parts, ident)) == before
        assert await numbers(parts, ident) == [1, 2, 3]


@pytest.mark.anyio
async def test_a_fill_in_use_or_unknown_keeps_the_album_and_its_match(
    navidrome: NavidromeInstance, state: Path
) -> None:
    title = "I Editions Kept"
    ident = await owned_album(navidrome, title)
    old, new = edition("i-kept-a", 3, title), edition("i-kept-b", 4, title)
    async with engine_for(navidrome, state) as parts:
        fills = fills_for(parts.engine, parts.store, parts.navidrome, old, new)
        await fills.choose(ident, str(old.ref))
        before = dict(await match(parts, ident))

        def answering(removal: Removal) -> Any:
            async def take_out(ref: str, also: Statements) -> Removal:
                assert ref == str(old.ref)
                return removal

            return take_out

        with pytest.raises(InUse) as kept:
            await fills.change(ident, str(new.ref), answering(Removal(kept=["played", "rated"])))
        assert kept.value.uses == ["played", "rated"]
        with pytest.raises(ChoiceRefused, match="no records of use"):
            await fills.change(ident, str(new.ref), answering(Removal(refused="no records of use")))
        with pytest.raises(ChoiceRefused, match="added no songs that could be taken out"):
            await fills.change(ident, str(new.ref), answering(Removal()))
        assert dict(await match(parts, ident)) == before
        assert await numbers(parts, ident) == [1, 2, 3]
        # The engine's own records of a use keep it too, whoever asks.
        await parts.store.execute(
            "UPDATE placeholders SET last_used_at = 1 WHERE release_ref = ?", [str(old.ref)]
        )
        with pytest.raises(InUse, match="streamed or downloaded"):
            await fills.change(
                ident, str(new.ref), cleanup_for(parts.engine, navidrome, state, fills).take_out_now
            )
        assert dict(await match(parts, ident)) == before
        assert await numbers(parts, ident) == [1, 2, 3]


@pytest.mark.anyio
async def test_a_release_that_adds_nothing_leaves_the_album_complete(
    navidrome: NavidromeInstance, state: Path
) -> None:
    title = "I Editions Complete"
    ident = await owned_album(navidrome, title, 2)
    old, new = edition("i-comp-a", 4, title), edition("i-comp-b", 2, title)
    async with engine_for(navidrome, state) as parts:
        fills = fills_for(parts.engine, parts.store, parts.navidrome, old, new)
        await fills.choose(ident, str(old.ref))
        assert await numbers(parts, ident) == [1, 2, 3, 4]
        decision = await fills.change(
            ident, str(new.ref), cleanup_for(parts.engine, navidrome, state, fills).take_out_now
        )
        assert (decision.outcome, decision.removed, decision.added) == ("complete", 2, 0)
        found = await match(parts, ident)
        assert (found["outcome"], found["release_ref"], found["plan"]) == (
            "complete", str(new.ref), None)  # fmt: skip
        assert await numbers(parts, ident) == [1, 2]
        filling = await parts.store.fetchall(
            "SELECT ref FROM releases WHERE owned_album_id = ?", [ident]
        )
        assert filling == []


@pytest.mark.anyio
async def test_a_release_numbered_past_an_owned_song_keeps_its_numbers(
    navidrome: NavidromeInstance, state: Path
) -> None:
    """The chosen release may put a song where an owned one is (a picked release keeps its
    numbering): allowed while picks are on, refused while they are off - before anything
    is taken out."""
    title = "I Editions Numbered"
    write_album(
        navidrome.music,
        Album(EDITIONS_ARTIST, title, (Track(f"{title} Song 3", 1, seconds=3),)),
    )
    navidrome.scan(full=True)
    albums = navidrome.client().ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})
    ident = next(str(a["id"]) for a in albums["albumList2"]["album"] if a["name"] == title)
    old, new = edition("i-num-a", 3, title), edition("i-num-b", 4, title)
    async with engine_for(navidrome, state) as parts:
        fills = fills_for(parts.engine, parts.store, parts.navidrome, old, new)
        await fills.choose(ident, str(old.ref))  # its songs 1 and 2 beside the owned "Song 3" at 1
        assert await numbers(parts, ident) == [1, 1, 2]
        fills.auto_pick = False
        with pytest.raises(ChoiceRefused, match="takes an owned track's position"):
            await fills.change(
                ident, str(new.ref), cleanup_for(parts.engine, navidrome, state, fills).take_out_now
            )
        assert await numbers(parts, ident) == [1, 1, 2]
        fills.auto_pick = True
        decision = await fills.change(
            ident, str(new.ref), cleanup_for(parts.engine, navidrome, state, fills).take_out_now
        )
        assert (decision.outcome, decision.removed, decision.added) == ("filled", 2, 3)
        assert await numbers(parts, ident) == [1, 1, 2, 4]
