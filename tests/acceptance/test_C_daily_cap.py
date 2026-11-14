"""The daily cap on adding (``[catalog] user_releases_per_day``) at the app-facing paths:
an account that is not an administrator adds releases up to the cap; at it, every request
that would add one is answered with a Subsonic error (JSON and XML) and writes nothing,
while catalog views, music in the library and plain streams go on as ever. Administrators
and the installation's own work are not counted, and requests at the same time never pass
the cap.

The catalog is invented: small releases served by ID, audio from a fake add-on.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from xml.etree import ElementTree

import anyio
import pytest

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.placeholders.daily_cap import MESSAGE, DailyLimitReached, adding_for
from tests.acceptance.test_C_interception import CLIENT_INFO
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeTrack
from tests.harness.replay import Replay
from tests.harness.subsonic import SubsonicClient

LENA = ("lena", "lena-password")
MILO = ("milo", "milo-password")
COVER = "https://covers.demo.invalid/cap{number}/{{w}}x{{h}}.jpg"  # one for each release


def release(number: int) -> CatalogRelease:
    ref = CatalogRef("demo", f"cap{number}")
    title, artist = f"Capped Letters {number}", f"Invented Limit {number}"
    return CatalogRelease(
        ref,
        title,
        artist,
        ReleaseKind.ALBUM,
        "2021",
        artwork_template=COVER.format(number=number),
        tracks=tuple(
            CatalogTrack(
                CatalogRef("demo", f"cap{number}song{n}"),
                f"{title} Song {n}",
                artist,
                3000,
                number=n,
                album=ref,
                album_title=title,
                isrc=f"ZZCAP{number:04d}{n:03d}",
            )
            for n in (1, 2)
        ),
    )


RELEASES = {f"cap{n}": release(n) for n in range(1, 41)}
SLOW: dict[str, float] = {}  # release -> how long the catalog takes to answer for it
SONGS = {t.ref.id: t for r in RELEASES.values() for t in r.tracks}


def song(number: int, track: int = 1) -> str:
    return f"sh.tr.demo.cap{number}song{track}"


def album(number: int) -> str:
    return f"sh.al.demo.cap{number}"


def ref(number: int) -> str:
    return f"demo:cap{number}"


@pytest.fixture(scope="module")
def replay() -> Replay:
    return Replay()


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory({"ND_ENABLESHARING": "true"})
    catalog = replay.catalog()

    async def by_album(ident: str) -> CatalogRelease:
        replay.log.append(f"album {ident}")  # the catalog was asked for the release
        if ident in SLOW:  # a catalog that takes its time over this release
            await anyio.sleep(SLOW[ident])
        if ident not in RELEASES:
            raise CatalogError("not_found", "no such album")
        return RELEASES[ident]

    async def by_song(ident: str) -> CatalogTrack:
        if ident not in SONGS:
            raise CatalogError("not_found", "no such song")
        return SONGS[ident]

    async def search(term: str, limit: int = 20) -> SearchResults:
        return SearchResults()

    catalog.album, catalog.song, catalog.search = by_album, by_song, search  # type: ignore[method-assign]
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("daily-cap"),
        catalog=catalog,
        catalog_settings={"user_releases_per_day": 2},
        warm_ahead_depth=0,
    ) as w:
        w.users = {  # type: ignore[attr-defined]
            name: nd.create_user(name, password) for name, password in (LENA, MILO)
        }
        addon = w.addon("Source")
        w.add_source(addon)
        audio = w.audio("capped-audio")
        for track in SONGS.values():
            assert track.isrc
            addon.add(FakeTrack(isrc=track.isrc, audio=audio))
        yield w


def call(world: DeliveryWorld, work: Any) -> Any:
    return world.server.call(work)


def user_id(world: DeliveryWorld, name: str) -> str:
    return str(world.users[name])  # type: ignore[attr-defined]


def additions(world: DeliveryWorld) -> list[tuple[str, str]]:
    """(account name, release) for every counted addition."""
    names = {ident: name for name, ident in world.users.items()}  # type: ignore[attr-defined]
    rows = call(
        world,
        lambda: world.services.store.fetchall(
            "SELECT user_id, release_ref FROM release_additions ORDER BY release_ref"
        ),
    )
    return [(names.get(str(r["user_id"]), str(r["user_id"])), str(r["release_ref"])) for r in rows]


def in_library(world: DeliveryWorld, number: int) -> bool:
    row = call(
        world,
        lambda: world.services.store.fetchone(
            "SELECT 1 FROM releases WHERE ref = ?", [ref(number)]
        ),
    )
    return row is not None


def nothing_of(world: DeliveryWorld, number: int) -> bool:
    """Nothing of the release is in the library: no record, nothing pending, no file."""

    async def rows() -> int:
        store = world.services.store
        found = 0
        for table in ("releases", "placeholders", "pending_placeholders"):
            column = "ref" if table == "releases" else "release_ref"
            row = await store.fetchone(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?",  # noqa: S608
                [ref(number)],
            )
            found += int(row["n"])
        return found

    files = [p for p in world.placeholder_files() if f"Capped Letters {number} " in p.name]
    return call(world, rows) == 0 and not files


@pytest.fixture(autouse=True)
def cap(world: DeliveryWorld) -> Iterator[Any]:
    """Each test starts with the configured cap (2) and no additions counted."""
    limiter = world.services.daily_cap
    assert limiter is not None and limiter.limit == 2  # from [catalog] user_releases_per_day
    call(world, lambda: world.services.store.execute("DELETE FROM release_additions"))
    yield limiter
    limiter.limit = 2
    assert not limiter._held  # every place taken was given up


@pytest.fixture
def lena(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    client = SubsonicClient(world.server.base_url, *LENA)
    yield client
    client.close()


@pytest.fixture
def milo(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    client = SubsonicClient(world.server.base_url, *MILO)
    yield client
    client.close()


def refusal(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["subsonic-response"]
    assert body["status"] == "failed", body
    return dict(body["error"])


def test_an_account_adds_up_to_its_cap_and_is_then_refused_in_json_and_xml(
    world: DeliveryWorld, lena: SubsonicClient, replay: Replay
) -> None:
    lena.ok("star", {"id": song(1)})
    lena.ok("scrobble", {"id": song(2), "submission": "false"})
    assert in_library(world, 1) and in_library(world, 2)
    assert additions(world) == [("lena", ref(1)), ("lena", ref(2))]
    covers = replay.artwork_requests
    asked = len(replay.log)
    # At the cap: the same error in JSON and XML, saying why; nothing written; the catalog
    # not even asked for the release or its cover.
    error = refusal(lena.request("star", {"id": song(3)}))
    assert error == {"code": 0, "message": MESSAGE}
    assert "daily limit of added releases" in MESSAGE
    answer = lena.request("star", {"id": song(3)}, fmt="xml")
    assert answer.status_code == 200 and answer.headers["content-type"].startswith(
        ("application/xml", "text/xml")
    )
    root = ElementTree.fromstring(answer.content)  # noqa: S314 (Shijhon's own answer)
    assert root.attrib["status"] == "failed"
    failed = root.find("{http://subsonic.org/restapi}error")
    assert failed is not None and failed.attrib == {"code": "0", "message": MESSAGE}
    assert nothing_of(world, 3)
    assert replay.artwork_requests == covers
    assert "album cap3" not in replay.log[asked:]
    assert additions(world) == [("lena", ref(1)), ("lena", ref(2))]
    # Catalog views still show everything, and what is in the library works as ever.
    shown = lena.ok("getAlbum", {"id": album(3)})["album"]
    assert shown["name"] == "Capped Letters 3" and len(shown["song"]) == 2
    assert lena.ok("getSong", {"id": song(3)})["song"]["title"] == "Capped Letters 3 Song 1"
    lena.ok("star", {"id": song(1, 2)})  # another song of a release in the library
    lena.ok("setRating", {"id": song(2, 2), "rating": "5"})
    played = lena.request("stream", {"id": song(1)})
    assert played.status_code == 200 and played.content[:4] == b"fLaC"
    # A plain stream of a catalog song adds nothing (it never did): it plays as ever.
    for params in ({"id": song(3)}, {"id": song(3), "format": "raw"}):
        plain = lena.request("stream", params)
        assert plain.status_code == 200 and plain.content[:4] == b"fLaC"
    assert nothing_of(world, 3)
    # Taking a star or a rating back needs no room.
    lena.ok("unstar", {"id": song(3)})
    lena.ok("setRating", {"id": song(3), "rating": "0"})


def decision(client: SubsonicClient, media: str) -> Any:
    return client.http.post(
        f"{client.base_url}/rest/getTranscodeDecision",
        params=[*client.auth_params(), ("f", "json"), ("mediaId", media), ("mediaType", "song")],
        content=json.dumps(CLIENT_INFO),
        headers={"content-type": "application/json"},
    )


S, A = song(5), album(5)
# Every request that adds a release (``views.commits.COMMITS``, but for the jukebox, which
# is off and places nothing, and what takes a star or a rating back).
ADDING: list[tuple[str, str, Any]] = [
    ("a starred song", "star", {"id": S}),
    ("a starred album", "star", {"albumId": A}),
    ("a starred album by id", "star", {"id": A}),
    ("a rating", "setRating", {"id": S, "rating": "4"}),
    ("a play reported", "scrobble", {"id": S}),
    ("now playing", "scrobble", {"id": S, "submission": "false"}),
    ("a playback report", "reportPlayback",
     {"mediaId": S, "mediaType": "song", "positionMs": "0", "state": "playing"}),
    ("a new playlist", "createPlaylist", [("name", "Capped"), ("songId", S)]),
    ("a play queue", "savePlayQueue", {"id": S, "current": S}),
    ("a play queue by index", "savePlayQueueByIndex", [("id", S), ("currentIndex", "0")]),
    ("a bookmark", "createBookmark", {"id": S, "position": "1500"}),
    ("a share", "createShare", {"id": S}),
    ("a share of an album", "createShare", {"id": A}),
    ("a download", "download", {"id": S}),
    ("a stream in another format", "stream", {"id": S, "format": "mp3"}),
    ("a stream at a lower bitrate", "stream", {"id": S, "maxBitRate": "128"}),
    ("a transcoded stream", "getTranscodeStream",
     {"mediaId": S, "mediaType": "song", "transcodeParams": "x"}),
]  # fmt: skip


@pytest.fixture
def at_cap(world: DeliveryWorld, cap: Any) -> None:
    """lena has added as many releases as she may (counted an hour ago)."""

    async def fill() -> None:
        for number in (901, 902):
            await world.services.store.execute(
                "INSERT INTO release_additions (user_id, release_ref, added_at) VALUES (?, ?, ?)",
                [user_id(world, "lena"), f"demo:earlier{number}", time.time() - 3600],
            )

    call(world, fill)


@pytest.mark.parametrize(("label", "method", "params"), ADDING, ids=[a[0] for a in ADDING])
def test_every_request_that_adds_a_release_is_refused_at_the_cap(
    world: DeliveryWorld,
    lena: SubsonicClient,
    replay: Replay,
    at_cap: None,
    label: str,
    method: str,
    params: Any,
) -> None:
    covers, asked = replay.artwork_requests, len(replay.log)
    for fmt in ("json", "xml"):
        answer = lena.request(method, params, fmt=fmt)  # type: ignore[arg-type]
        assert answer.status_code == 200, label
        if fmt == "json":
            assert refusal(answer) == {"code": 0, "message": MESSAGE}, label
        else:
            root = ElementTree.fromstring(answer.content)  # noqa: S314
            failed = root.find("{http://subsonic.org/restapi}error")
            assert failed is not None and failed.attrib["message"] == MESSAGE, label
    assert nothing_of(world, 5), label
    assert len(additions(world)) == 2, label
    # Refused before the catalog is asked for the release or its cover.
    assert replay.artwork_requests == covers, label
    assert "album cap5" not in replay.log[asked:], label


def test_a_playlist_entry_and_a_transcode_decision_are_refused_at_the_cap(
    world: DeliveryWorld, lena: SubsonicClient, at_cap: None
) -> None:
    made = lena.ok("createPlaylist", {"name": "Before the cap"})["playlist"]["id"]
    added = lena.request("updatePlaylist", {"playlistId": made, "songIdToAdd": song(5)})
    assert refusal(added) == {"code": 0, "message": MESSAGE}
    assert not lena.ok("getPlaylist", {"id": made})["playlist"].get("entry")
    assert refusal(decision(lena, song(5))) == {"code": 0, "message": MESSAGE}
    assert nothing_of(world, 5)
    lena.ok("deletePlaylist", {"id": made})


def test_an_administrator_is_not_limited_and_not_counted(
    world: DeliveryWorld, lena: SubsonicClient, at_cap: None
) -> None:
    admin = world.client()
    for number in (6, 7, 8):  # more than the cap
        admin.ok("star", {"id": song(number)})
        assert in_library(world, number)
    assert len(additions(world)) == 2  # lena's earlier ones only
    # What an administrator added is in the library for everyone: no room needed.
    lena.ok("star", {"id": song(6)})
    lena.ok("createPlaylist", [("name", "Shared music"), ("songId", song(7)), ("songId", song(8))])


def test_without_a_cap_nothing_is_limited_recorded_or_asked(
    world: DeliveryWorld, lena: SubsonicClient, cap: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default. Nothing is counted or kept, and the cap's own work does not run at
    all: no place is asked for or looked at, nothing is read or removed in the database,
    Navidrome is not asked for a role, and the engine keeps nothing for a write."""
    cap.limit = 0
    engine, commits = world.services.engine, world.services.commits
    assert commits is not None
    ran: list[str] = []

    def never(name: str) -> Any:
        async def called(*args: Any, **kwargs: Any) -> Any:
            ran.append(name)
            raise AssertionError(f"{name} ran while no cap is set")

        return called

    for name in ("reserve", "check", "_take", "prune", "_used", "_counted", "is_admin"):
        monkeypatch.setattr(cap, name, never(name))
    monkeypatch.setattr(commits, "new_releases", never("new_releases"))
    for number in (9, 10, 11):
        lena.ok("star", {"id": song(number)})
        assert in_library(world, number)
    made = lena.ok(
        "createPlaylist", [("name", "No cap"), ("songId", song(34)), ("songId", song(35))]
    )["playlist"]
    assert len(made["entry"]) == 2
    # A release taken out and used again by its old ID comes back the same way.
    old = made["entry"][0]["id"]
    lena.ok("deletePlaylist", {"id": made["id"]})
    assert call(world, lambda: engine.remove_release(ref(34))).removed == 2
    lena.ok("scrobble", {"id": old, "submission": "false"})
    assert in_library(world, 34)
    assert ran == [] and not engine._places and not cap._held
    assert additions(world) == []
    assert await_standing(world, cap) is None


def await_standing(world: DeliveryWorld, cap: Any) -> Any:
    return call(world, lambda: cap.standing(user_id(world, "lena"), admin=False))


def test_a_request_that_names_several_releases_adds_all_of_them_or_none(
    world: DeliveryWorld, lena: SubsonicClient
) -> None:
    three = [("name", "Too many"), *(("songId", song(n)) for n in (12, 13, 14))]
    assert refusal(lena.request("createPlaylist", three)) == {"code": 0, "message": MESSAGE}
    assert all(nothing_of(world, n) for n in (12, 13, 14)) and additions(world) == []
    assert not [
        p
        for p in lena.ok("getPlaylists")["playlists"].get("playlist", [])
        if p["name"] == "Too many"
    ]
    # Two fit; with songs of a release in the library, which needs no room.
    lena.ok("star", {"id": song(12)})
    fitting = [("name", "Fits"), *(("songId", song(n, 2)) for n in (12, 13))]
    made = lena.ok("createPlaylist", fitting)["playlist"]
    assert len(made["entry"]) == 2 and in_library(world, 13) and nothing_of(world, 14)
    assert additions(world) == [("lena", ref(12)), ("lena", ref(13))]


def test_requests_at_the_same_time_never_pass_the_cap(
    world: DeliveryWorld, lena: SubsonicClient, milo: SubsonicClient, cap: Any
) -> None:
    """One place left for each of two accounts, four releases asked for at once: each
    account adds exactly one, whichever request came first."""
    cap.limit = 1
    wanted = {"lena": (15, 16), "milo": (17, 18)}

    def star(pair: tuple[str, int]) -> bool:
        name, number = pair
        client = SubsonicClient(world.server.base_url, *(LENA if name == "lena" else MILO))
        try:
            body = client.request("star", {"id": song(number)}).json()["subsonic-response"]
        finally:
            client.close()
        if body["status"] != "ok":
            assert body["error"] == {"code": 0, "message": MESSAGE}
        return bool(body["status"] == "ok")

    pairs = [(name, number) for name, numbers in wanted.items() for number in numbers]
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(pairs, pool.map(star, pairs), strict=True))
    for name, numbers in wanted.items():
        added = [n for n in numbers if outcomes[(name, n)]]
        assert len(added) == 1, (name, outcomes)
        other = next(n for n in numbers if n != added[0])
        assert in_library(world, added[0]) and nothing_of(world, other)
    assert sorted(name for name, _ in additions(world)) == ["lena", "milo"]


def test_the_write_itself_refuses_a_release_over_the_cap(
    world: DeliveryWorld, at_cap: None
) -> None:
    """The engine's own check, behind every path: a release written for an account at its
    cap is refused before anything is written; for nobody (the fill policy, the library
    pass) and for an administrator it is written and not counted."""
    engine = world.services.engine

    async def write(number: int, user: str | None, admin: bool | None = False) -> None:
        with adding_for(user, admin=admin):
            await engine.materialize(RELEASES[f"cap{number}"])

    with pytest.raises(DailyLimitReached):
        call(world, lambda: write(19, user_id(world, "lena")))
    assert nothing_of(world, 19) and not engine.writing()
    call(world, lambda: write(19, None))
    call(world, lambda: write(20, user_id(world, "milo"), True))
    # ... and an account whose role the caller did not say is asked of Navidrome.
    call(world, lambda: write(21, call_admin_id(world), None))
    assert in_library(world, 19) and in_library(world, 20) and in_library(world, 21)
    assert len(additions(world)) == 2
    call(world, lambda: write(22, user_id(world, "milo"), None))
    assert ("milo", ref(22)) in additions(world)


def call_admin_id(world: DeliveryWorld) -> str:
    users = world.nd.native("GET", "user").json()
    return str(next(u["id"] for u in users if u["isAdmin"]))


def test_the_page_setting_applies_at_once(world: DeliveryWorld, lena: SubsonicClient) -> None:
    """Saved on the Catalog page, the cap applies from the next request, without a restart."""
    from shijhon.dashboard import live

    assert live.is_live("catalog", "user_releases_per_day")
    section = world.app.settings.catalog.model_copy(update={"user_releases_per_day": 1})
    applied = call(
        world,
        lambda: live.apply(world.services, "catalog", ["user_releases_per_day"], section),
    )
    assert applied == ["user_releases_per_day"]
    lena.ok("star", {"id": song(23)})
    assert refusal(lena.request("star", {"id": song(24)}))["message"] == MESSAGE
    assert in_library(world, 23) and nothing_of(world, 24)


def test_a_request_keeps_its_places_while_it_waits_for_the_catalog(
    world: DeliveryWorld, lena: SubsonicClient
) -> None:
    """Two releases in one request, one place used, a cap of 3: the request takes both
    places before its first write. A competing addition that arrives while the catalog
    still answers for the first release finds no room and is refused; the request adds
    both - never the error with one of its releases left in the library."""
    cap = world.services.daily_cap
    assert cap is not None
    cap.limit = 3
    lena.ok("star", {"id": song(25)})
    SLOW["cap26"] = 1.5
    try:
        both = [("name", "Both or none"), ("songId", song(26)), ("songId", song(27))]
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(lambda: lena.request("createPlaylist", both).json())
            deadline = time.monotonic() + 10
            while not cap._held and time.monotonic() < deadline:  # its places are taken
                time.sleep(0.02)
            assert sum(cap._held.values()) == 2
            competing = SubsonicClient(world.server.base_url, *LENA)
            try:
                assert refusal(competing.request("star", {"id": song(28)})) == {
                    "code": 0,
                    "message": MESSAGE,
                }
            finally:
                competing.close()
            answer = waiting.result(30)["subsonic-response"]
        assert answer["status"] == "ok" and len(answer["playlist"]["entry"]) == 2
        assert in_library(world, 26) and in_library(world, 27) and nothing_of(world, 28)
        assert additions(world) == [("lena", ref(25)), ("lena", ref(26)), ("lena", ref(27))]
    finally:
        SLOW.clear()


def test_old_ids_and_catalog_items_of_one_request_fit_together_or_not_at_all(
    world: DeliveryWorld, lena: SubsonicClient
) -> None:
    """A request that uses an old ID of a release taken out and names a catalog song of a
    release not in the library needs two places: with one, nothing is added back or
    added and the request is answered with the cap's error; with two, both are there."""
    cap, engine = world.services.daily_cap, world.services.engine
    assert cap is not None
    admin = world.client()
    admin.ok("scrobble", {"id": song(29), "submission": "false"})  # in the library, unused
    old = str(admin.ok("getSong", {"id": song(29)})["song"]["id"])
    assert call(world, lambda: engine.remove_release(ref(29))).removed == 2
    assert engine.removed_release(old) == ref(29)
    lena.ok("star", {"id": song(30)})  # one of lena's two places
    mixed = [("name", "Old and new"), ("songId", old), ("songId", song(31))]
    assert refusal(lena.request("createPlaylist", mixed)) == {"code": 0, "message": MESSAGE}
    assert not in_library(world, 29) and nothing_of(world, 31)
    assert engine.restore_due(ref(29)) and not cap._held
    assert additions(world) == [("lena", ref(30))]
    playlists = lena.ok("getPlaylists")["playlists"].get("playlist", [])
    assert "Old and new" not in [p["name"] for p in playlists]
    cap.limit = 3
    made = lena.ok("createPlaylist", mixed)["playlist"]
    assert made["entry"][0]["id"] == old and len(made["entry"]) == 2
    assert in_library(world, 29) and in_library(world, 31)
    assert additions(world) == [("lena", ref(29)), ("lena", ref(30)), ("lena", ref(31))]


def test_an_administrator_demoted_while_the_catalog_answers_is_counted(
    world: DeliveryWorld,
) -> None:
    """The role is read when a place is taken, not kept from the request's start: an
    administrator whose request waits for the catalog and who is demoted meanwhile adds
    under the cap like anyone - here at it, so nothing is written."""
    cap = world.services.daily_cap
    assert cap is not None
    created = world.nd.native(
        "POST",
        "user",
        json={"userName": "boss", "name": "boss", "password": "boss-password", "isAdmin": True},
    ).json()
    boss = str(created["id"])  # (an administrator reads every library)
    client = SubsonicClient(world.server.base_url, "boss", "boss-password")

    async def earlier() -> None:
        for number in (1, 2):
            await world.services.store.execute(
                "INSERT INTO release_additions (user_id, release_ref, added_at) VALUES (?, ?, ?)",
                [boss, f"demo:boss{number}", time.time() - 3600],
            )

    call(world, earlier)
    seconds = cap.role_seconds
    assert seconds == 5.0  # (never longer than a credential check is reused: 60 s here)
    cap.role_seconds = 0.3  # (the test's wait is shorter than five seconds)
    SLOW["cap33"] = 1.5
    try:
        client.ok("star", {"id": song(32)})  # an administrator: not limited, not counted
        assert in_library(world, 32)
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(lambda: client.request("star", {"id": song(33)}))
            time.sleep(0.5)  # the catalog is being asked for the release
            user = world.nd.native("GET", f"user/{boss}").json()
            world.nd.native("PUT", f"user/{boss}", json={**user, "isAdmin": False})
            assert refusal(waiting.result(30)) == {"code": 0, "message": MESSAGE}
        assert nothing_of(world, 33)
        rows = call(
            world,
            lambda: world.services.store.fetchall(
                "SELECT release_ref FROM release_additions WHERE user_id = ?", [boss]
            ),
        )
        assert sorted(str(r["release_ref"]) for r in rows) == ["demo:boss1", "demo:boss2"]
    finally:
        SLOW.clear()
        cap.role_seconds = seconds
        client.close()
