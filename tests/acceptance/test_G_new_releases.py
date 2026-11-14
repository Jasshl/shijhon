"""Suite G (continued) - new releases of followed artists in the newest albums.

An account that turned new releases on gets, at the top of ``getAlbumList2`` with
``type=newest``, the releases its daily check saved: the followed artists' releases the
library lacks that came out within the window, as catalog albums exactly like a
search's. The list is answered from what was saved (no catalog request), pages
consistently, and stays Navidrome's own for every other account, list type and format
that gets no catalog entries (JSONP, ``HEAD``, the folder-based ``getAlbumList``).

Every test passes alone, in any order and with the file split across workers. What the
tests read is set up once per world and never changed: the administrator favorited one
artist, a second account favorited the same artist and played an album of another, and
the administrator follows its artists (``scene``). The tests about an album that got
into the library have a world of their own in which that happened (``used``). A test
that needs more - the second account following, the feature off - sets it up itself and
undoes it (``following``). Checks are the service's own background checks, waited for
(``wait_checked``).
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import anyio
import pytest

from shijhon.new_releases import NewReleases, Read
from tests.acceptance.test_J_contract import ARTIST, OWNED, TERM
from tests.conftest import NavidromeFactory
from tests.harness import xsd
from tests.harness.collection import prepare_collection, refresh_collection
from tests.harness.dashboard import PATH, Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient
from tests.harness.xsd import same_data

HOST = {"host": "music.test"}
NS = "{http://subsonic.org/restapi}"
RELEASES = "artists/900000147/releases"  # the duo fixtures' artist
WOLVES, WOLVES_EDITION, RESTLESS, COLORS, STATION, HOLLOW = (
    "900000296",  # an album and its much longer anniversary edition: a card each
    "900000311",
    "900000387",
    "900000392",
    "900000391",
    "900000374",
)
BAND, BAND_RELEASES, LUNAR = "BrokenMeadow", "artists/900000205/releases", "900000448"
# An album of a second artist, which the second account played and nobody favorited.
PLAYED = Album(BAND, "Field Notes", (Track("Low Fences", 1),))
OTHER = "listener"
PASSWORD = "fixture-password"
NEW = [f"sh.al.demo.{WOLVES}", f"sh.al.demo.{WOLVES_EDITION}", f"sh.al.demo.{RESTLESS}"]
BAND_NEW = f"sh.al.demo.{LUNAR}"


def dated(replay: Replay, release: str, day: date | str, path: str = RELEASES) -> None:
    for body in replay.by_path[path]["body"]:
        if body["ref"]["id"] == release:
            body["release_date"] = day if isinstance(day, str) else day.isoformat()
            return
    raise AssertionError(release)


def new_replay() -> Replay:
    """The demo catalog with releases of the two artists dated around today (its records
    are read anew for every replay: nothing is shared between worlds)."""
    replay = Replay()
    replay.aliases[TERM] = str(fixture("search-single-vs-album")["params"]["term"])
    replay.aliases[BAND.lower()] = str(fixture("search-anniversary")["params"]["term"])
    today = date.today()
    dated(replay, WOLVES, today - timedelta(days=3))  # new, and so is its long edition
    dated(replay, WOLVES_EDITION, today - timedelta(days=3))
    dated(replay, RESTLESS, today - timedelta(days=12))  # new, older
    dated(replay, COLORS, today + timedelta(days=5))  # announced: not out yet
    dated(replay, STATION, today - timedelta(days=45))  # past the window
    dated(replay, HOLLOW, today - timedelta(days=1))  # an edition of an album the library has
    dated(replay, LUNAR, today - timedelta(days=2), BAND_RELEASES)  # new, of the second artist
    return replay


@dataclass
class Scene:
    """One world as every test finds it: the administrator follows its favorite artist
    and was checked once; the second account does not follow."""

    world: DeliveryWorld
    replay: Replay
    owner: str  # the administrator's user ID
    other: str  # the second account's
    first_check: list[str] = field(default_factory=list)  # that check's catalog requests

    @property
    def releases(self) -> NewReleases:
        found = self.world.services.new_releases
        assert found is not None
        return found

    def call(self, work: Callable[[], Any]) -> Any:
        """Run a coroutine on the app's own event loop."""
        return self.world.server.call(work)

    def app(self, user: str = ADMIN_USER, password: str = ADMIN_PASSWORD) -> SubsonicClient:
        """An app's client through Shijhon."""
        return SubsonicClient(
            self.world.server.base_url, user, password, headers=HOST, client="NewClient"
        )

    def navidrome(self) -> SubsonicClient:
        return self.world.nd.client()


def wait_checked(world: DeliveryWorld, owner: str, timeout: float = 60.0) -> None:
    """Until the account's check (the service's own, in the background) has finished."""
    releases = world.services.new_releases
    assert releases is not None
    deadline = time.monotonic() + timeout
    while True:
        status = world.server.call(lambda: releases.status(owner))
        assert status.enabled and status.state == "", status
        if status.checked_at is not None and not status.checking:
            return
        assert time.monotonic() < deadline, "the account's check did not finish"
        time.sleep(0.05)


@contextmanager
def following(scene: Scene, owner: str, user: str) -> Iterator[None]:
    """The account follows its artists and was checked; off again afterwards, with
    nothing of it kept."""
    scene.call(lambda: scene.releases.turn_on(owner, user))
    try:
        wait_checked(scene.world, owner)
        yield
    finally:
        scene.call(lambda: scene.releases.turn_off(owner))


@contextmanager
def held_checks(scene: Scene) -> Iterator[Callable[[], None]]:
    """Checks wait before they read an account, until the function given is called."""
    releases = scene.releases
    inner = releases.account

    async def hold() -> anyio.Event:
        gate = anyio.Event()

        async def held(owner: str) -> Read:
            await gate.wait()
            return await inner(owner)

        releases.account = held
        return gate

    gate = scene.call(hold)

    def release() -> None:
        async def go() -> None:
            gate.set()

        scene.call(go)

    try:
        yield release
    finally:
        release()
        releases.account = inner


@contextmanager
def scene_of(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory, name: str
) -> Iterator[Scene]:
    replay = new_replay()
    nd = navidrome_factory()
    for album in (*OWNED, PLAYED):
        write_album(nd.music, album)
    nd.scan(full=True)
    nd.wait_artwork()
    nd.create_user(OTHER, PASSWORD)
    # Each account's favorites and plays, as Navidrome has them before any test.
    for user, password in ((None, None), (OTHER, PASSWORD)):
        account = nd.client(user, password)
        artist = account.ok("search3", {"query": ARTIST, "albumCount": 0, "songCount": 0})
        account.ok("star", {"artistId": artist["searchResult3"]["artist"][0]["id"]})
        if user == OTHER:
            song = account.ok("search3", {"query": "Low Fences", "artistCount": 0})
            for _ in range(2):
                account.ok("scrobble", {"id": song["searchResult3"]["song"][0]["id"]})
        account.close()
    with delivery_world(nd, tmp_path_factory.mktemp(name), catalog=replay.catalog()) as w:
        owner = prepare_collection(nd, w.server)
        other = prepare_collection(nd, w.server, user=OTHER, password=PASSWORD)
        scene = Scene(w, replay, owner, other)
        before = len(replay.log)
        scene.call(lambda: scene.releases.turn_on(owner, ADMIN_USER))
        wait_checked(w, owner)
        scene.first_check = replay.log[before:]
        yield scene


@pytest.fixture(scope="module")
def scene(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Scene]:
    """A world whose library no test changes."""
    with scene_of(navidrome_factory, tmp_path_factory, "new") as found:
        yield found


@dataclass
class Used:
    scene: Scene
    before: list[str]  # the administrator's newest albums before the use
    album: str  # the new release the administrator used


@pytest.fixture(scope="module")
def used(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Used]:
    """A world of its own in which the administrator used one of the new releases (a
    favorite), which wrote it into the library - for the tests of what follows from that."""
    with scene_of(navidrome_factory, tmp_path_factory, "used") as found:
        client = found.app()
        try:
            before = ids(newest(client, size=50))
            album = NEW[0]
            client.ok("star", {"albumId": album})
            deadline = time.monotonic() + 60
            while album in ids(newest(client, size=50)):
                assert time.monotonic() < deadline, "the album did not leave the list"
                refresh_collection(found.world.server, found.owner)
                time.sleep(0.5)
        finally:
            client.close()
        yield Used(found, before, album)


@pytest.fixture
def client(scene: Scene) -> Iterator[SubsonicClient]:
    c = scene.app()
    yield c
    c.close()


def newest(client: SubsonicClient, **params: Any) -> list[dict[str, Any]]:
    answer = client.ok("getAlbumList2", {"type": "newest", **params})
    return list(answer["albumList2"].get("album", []))


def ids(albums: list[dict[str, Any]]) -> list[str]:
    return [str(a["id"]) for a in albums]


def catalog_ids(albums: list[dict[str, Any]]) -> list[str]:
    return [i for i in ids(albums) if i.startswith("sh.")]


def followed(scene: Scene, owner: str) -> dict[str, tuple[bool, bool]]:
    """The account's followed artists: (a favorite's, among the most played)."""
    artists = scene.call(lambda: scene.releases.artists(owner))
    return {artist.name: (artist.favorite, artist.played) for artist in artists}


def kept_rows(scene: Scene, owner: str | None = None) -> int:
    """What the feature keeps, of one account or of all."""

    async def count() -> int:
        total = 0
        for table in ("new_release_accounts", "new_release_artists", "new_releases"):
            row = await scene.world.services.store.fetchone(
                f"SELECT COUNT(*) AS n FROM {table} WHERE ? IS NULL OR owner_id = ?",  # noqa: S608
                [owner, owner],
            )
            total += int(row["n"]) if row is not None else 0
        return total

    return int(scene.call(count))


# --- a library no test changes ------------------------------------------------------------


def test_the_check_reads_the_account_and_asks_the_catalog_once_per_artist(scene: Scene) -> None:
    releases, owner = scene.releases, scene.owner
    assert followed(scene, owner) == {ARTIST: (True, False)}
    # One search for the name and the one artist's list of releases.
    assert len(scene.first_check) == 2 and RELEASES in scene.first_check
    status = scene.call(lambda: releases.status(owner))
    assert status.enabled and status.checked_at is not None and status.state == ""
    # A second check a moment later uses the discography the first one saved.
    saved = len(scene.call(lambda: releases.releases(owner)))
    asked = len(scene.replay.log)
    scene.call(lambda: releases.check(owner))
    assert len(scene.call(lambda: releases.releases(owner))) == saved == len(NEW)
    assert len(scene.replay.log) == asked


def test_new_releases_lead_the_newest_albums(scene: Scene, client: SubsonicClient) -> None:
    before = len(scene.replay.log)
    own = ids(scene.navidrome().ok("getAlbumList2", {"type": "newest"})["albumList2"]["album"])
    albums = newest(client)
    # The newest release first; the one not out yet, the one past the window and the
    # edition of an album the library has stay out. Navidrome's list follows, as it is.
    assert ids(albums)[: len(NEW)] == NEW
    assert ids(albums)[len(NEW) :] == own
    assert len(scene.replay.log) == before  # answered from what was saved: no catalog request
    first = albums[0]
    assert first["name"] == "Iron Wolves" and first["artist"] == ARTIST
    assert first["created"].startswith((date.today() - timedelta(days=3)).isoformat())


def test_an_entry_is_the_one_the_artist_page_shows(client: SubsonicClient) -> None:
    listed = {a["id"]: a for a in newest(client)}
    artist = client.ok("search3", {"query": ARTIST, "albumCount": 0, "songCount": 0})
    page = client.ok("getArtist", {"id": artist["searchResult3"]["artist"][0]["id"]})["artist"]
    shared = [a for a in page["album"] if a["id"] in listed and a["id"].startswith("sh.al.")]
    assert [a["id"] for a in shared] and {a["id"] for a in shared} == set(NEW)
    for album in shared:  # the same catalog album, field for field
        assert album == listed[album["id"]]
    # ... and it opens as any catalog album does.
    opened = client.ok("getAlbum", {"id": NEW[0]})["album"]
    assert opened["id"] == NEW[0] and len(opened["song"]) == opened["songCount"] == 13


def test_pages_neither_repeat_nor_skip(scene: Scene, client: SubsonicClient) -> None:
    whole = ids(newest(client, size=500))
    assert len(whole) == len(set(whole)) == len(NEW) + len(OWNED) + 1
    for size in (1, 2, 3):
        paged: list[str] = []
        for offset in range(0, len(whole) + size, size):
            page = ids(newest(client, size=size, offset=offset))
            assert len(page) <= size
            paged += page
        assert paged == whole, size
    total = client.request("getAlbumList2", {"type": "newest", "size": 1})
    native = scene.navidrome().request("getAlbumList2", {"type": "newest", "size": 1})
    assert int(total.headers["x-total-count"]) == int(native.headers["x-total-count"]) + len(NEW)
    # A form request (POST) pages the same way.
    posted = client.request(
        "getAlbumList2", {"type": "newest", "size": 2, "offset": 2}, http_method="POST"
    ).json()["subsonic-response"]
    assert ids(posted["albumList2"]["album"]) == whole[2:4]
    assert newest(client, offset=len(whole)) == []


def test_the_xml_answer_is_valid_and_holds_the_same_data(client: SubsonicClient) -> None:
    for params in ({"size": 10}, {"size": 1}, {"size": 2, "offset": 1}, {"offset": 50}):
        asked = {"type": "newest", **params}
        as_json = client.request("getAlbumList2", asked, fmt="json").json()["subsonic-response"]
        as_xml = client.request("getAlbumList2", asked, fmt="xml")
        assert as_xml.status_code == 200
        assert as_xml.headers["content-type"] == "application/xml"
        assert xsd.valid(as_xml.content)
        root = ET.fromstring(as_xml.content)  # noqa: S314 - test data
        assert same_data(as_json, root, "getAlbumList2") == []
        listed = [a.get("id") for a in root.iter(NS + "album")]
        assert listed == ids(as_json["albumList2"].get("album", []))
    first = client.request("getAlbumList2", {"type": "newest"}, fmt="xml")
    assert [a.get("id") for a in ET.fromstring(first.content).iter(NS + "album")][:3] == NEW  # noqa: S314


def test_other_lists_formats_and_accounts_get_navidromes_answer(
    scene: Scene, client: SubsonicClient
) -> None:
    for kind in ("alphabeticalByName", "recent", "frequent", "random", "starred"):
        listed = client.ok("getAlbumList2", {"type": kind, "size": 50})["albumList2"]
        assert catalog_ids(listed.get("album", [])) == [], kind
    folders = client.ok("getAlbumList", {"type": "newest"})["albumList"].get("album", [])
    assert catalog_ids(folders) == []
    jsonp = client.request(
        "getAlbumList2", {"type": "newest", "f": "jsonp", "callback": "cb"}, fmt="xml"
    )
    assert jsonp.text.startswith("cb(") and "sh.al." not in jsonp.text
    head = client.request("getAlbumList2", {"type": "newest"}, http_method="HEAD")
    assert head.status_code == 200 and head.content == b""
    counted = scene.navidrome().request("getAlbumList2", {"type": "newest"}, http_method="HEAD")
    assert head.headers.get("x-total-count") == counted.headers.get("x-total-count")
    # The second account did not turn it on, though it has a favorite artist and plays.
    releases, other = scene.releases, scene.app(OTHER, PASSWORD)
    try:
        assert catalog_ids(newest(other, size=50)) == []
        # A name alone is no account: one that turned it on under that name, with another
        # user ID (a deleted account's name given to a new one), shows this one nothing.
        scene.call(lambda: releases.seen(scene.owner, OTHER))
        try:
            assert releases.account_of(OTHER) == scene.owner
            assert catalog_ids(newest(other, size=50)) == []
        finally:
            scene.call(lambda: releases.seen(scene.owner, ADMIN_USER))
    finally:
        other.close()
    assert catalog_ids(newest(client, size=50)) == NEW
    # A list of another music folder is Navidrome's (here: its error for one it lacks).
    assert client.error_code("getAlbumList2", {"type": "newest", "musicFolderId": 2}) == 70
    assert ids(newest(client, musicFolderId=1))[: len(NEW)] == NEW


def test_the_most_played_artists_are_followed_without_a_favorite(
    scene: Scene, client: SubsonicClient
) -> None:
    """Navidrome's own record of the account's plays decides: the artist of an album
    the account played is followed, and only for that account."""
    theirs = scene.app(OTHER, PASSWORD)
    try:
        with following(scene, scene.other, OTHER):
            assert followed(scene, scene.other) == {ARTIST: (True, False), BAND: (False, True)}
            assert catalog_ids(newest(theirs, size=50)) == [BAND_NEW, *NEW]
            # The administrator never played it: checked again, the band is not among
            # theirs, and neither is its release.
            scene.call(lambda: scene.releases.check(scene.owner))
            assert followed(scene, scene.owner) == {ARTIST: (True, False)}
            assert catalog_ids(newest(client, size=50)) == NEW
        # Off again, the second account's list is Navidrome's and nothing of it is kept.
        assert catalog_ids(newest(theirs, size=50)) == []
        assert kept_rows(scene, scene.other) == 0
    finally:
        theirs.close()


def test_turned_off_the_list_is_navidromes_and_nothing_is_kept(
    scene: Scene, client: SubsonicClient
) -> None:
    releases, owner = scene.releases, scene.owner
    assert catalog_ids(newest(client, size=50)) == NEW
    scene.call(lambda: releases.turn_off(owner))
    try:
        assert catalog_ids(newest(client, size=50)) == []
        assert kept_rows(scene) == 0
    finally:  # as every test finds it: on, and checked
        scene.call(lambda: releases.turn_on(owner, ADMIN_USER))
        wait_checked(scene.world, owner)
    assert catalog_ids(newest(client, size=50)) == NEW


# --- a library that got one of the new releases -------------------------------------------


def test_a_release_in_the_library_leaves_the_list(used: Used) -> None:
    scene = used.scene
    assert used.before[: len(NEW)] == NEW and used.album == NEW[0]  # it led the list
    client = scene.app()
    try:
        listed = newest(client, size=50)
        # Navidrome lists the album itself now, once; its other edition stands back for
        # it, as on the artist's page (the edition rules), and the other new release
        # still leads.
        assert catalog_ids(listed) == NEW[2:]
        assert ids(listed)[0] == NEW[2]
        assert [a["name"] for a in listed].count("Iron Wolves") == 1
        own_before = len(used.before) - len(NEW)
        assert len(listed) - 1 == own_before + 1  # one more of the library's own
        total = client.request("getAlbumList2", {"type": "newest", "size": 1})
        native = scene.navidrome().request("getAlbumList2", {"type": "newest", "size": 1})
        assert int(total.headers["x-total-count"]) == int(native.headers["x-total-count"]) + 1
    finally:
        client.close()


def test_an_album_another_account_added_still_leads_for_one_who_lacks_it(used: Used) -> None:
    """The administrator's use wrote the album into the library. The second account's
    selection does not show it, so its own list keeps the card, which stands for the
    library's copy as in a search."""
    scene = used.scene
    mine, theirs = scene.app(), scene.app(OTHER, PASSWORD)
    try:
        with following(scene, scene.other, OTHER):
            refresh_collection(scene.world.server, scene.other)
            listed = newest(theirs, size=50)
            assert catalog_ids(listed) == [BAND_NEW, NEW[0], NEW[2]]
            assert [a["name"] for a in listed].count("Iron Wolves") == 1  # the card alone
            # The administrator's list is its own: without the album it has.
            assert catalog_ids(newest(mine, size=50)) == NEW[2:]
    finally:
        mine.close()
        theirs.close()


def test_each_person_turns_it_on_and_sees_their_list_on_the_dashboard(used: Used) -> None:
    scene = used.scene
    releases, other = scene.releases, scene.other
    listener, admin = Browser(scene.world.server.base_url), Browser(scene.world.server.base_url)
    try:
        # A person without administrator rights lands on the page they asked for.
        signed = listener.sign_in(OTHER, PASSWORD, next=f"{PATH}/new-releases")
        assert signed.headers["location"] == f"{PATH}/new-releases"
        page = listener.get("new-releases").text
        assert 'role="switch" aria-checked="false"' in page and 'value="on"' in page
        assert "New from your artists" not in page and "<script" not in page
        assert scene.call(lambda: releases.status(other)).enabled is False
        with held_checks(scene) as go:  # (the first check waits until the page was read)
            on = listener.submit("new-releases", {"follow": "on"}, "new-releases/follow")
            assert on.status_code == 303 and on.headers["location"] == f"{PATH}/new-releases"
            assert releases.account_of(OTHER) == other
            page = listener.get("new-releases").text
            assert 'role="switch" aria-checked="true"' in page and 'value="off"' in page
            assert "Your artists are being checked for the first time." in page
            go()
            wait_checked(scene.world, other)
        page = listener.get("new-releases").text
        # Its favorite artist, and the one it played.
        assert "2 artists followed." in page and "Last checked" in page
        assert f'<li><span>{ARTIST}</span> <span class="field-help">favorite</span>' in page
        assert f'<li><span>{BAND}</span> <span class="field-help">most played</span>' in page
        # The list is the one the account's apps get: here with the album another
        # account added, which this one lacks.
        assert "3 releases from the last 30 days" in page
        assert (
            page.index(">Lunar 6 (Deluxe Edition)</p>")
            < page.index(">Iron Wolves</p>")
            < page.index("RESTLESS (Pale Mirrors 3)")
        )
        assert '<span class="tag">Single</span>' in page
        day = (date.today() - timedelta(days=3)).isoformat()
        assert f'<time datetime="{day}">' in page
        # The administrator's page shows the administrator's own list.
        assert admin.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        theirs = admin.get("new-releases").text
        assert "1 release from the last 30 days" in theirs and ">Iron Wolves</p>" not in theirs
        assert BAND not in theirs
        # Off again: the switch says so, and nothing of the account is kept.
        off = listener.submit("new-releases", {"follow": "off"}, "new-releases/follow")
        assert off.status_code == 303
        page = listener.get("new-releases").text
        assert 'aria-checked="false"' in page and ARTIST not in page
        assert kept_rows(scene, other) == 0
        assert scene.call(lambda: releases.status(scene.owner)).enabled
    finally:
        scene.call(lambda: releases.turn_off(other))
        listener.close()
        admin.close()
