"""A described playlist related to a playlist the owner has, from request to Navidrome.

Everything is real except the music (invented), the model (scripted, answering with the
strict schemas the real one gets) and the sound analyzer: a fake AudioMuse-AI with the
shapes of its source, reached through real ``[audiomuse]`` settings, so that the app's own
wiring, the sync of its analysis and the sound pool of the request are all exercised.

The request: "like Velvet Orbit but more livelier", with the library-context option. The
model sees the reference songs with sound facts and a profile, sound analysis adds library
songs the model did not name, the ordering answer rates each song, one is flagged and
replaced in the revision, and the playlist publishes with the chosen songs. With the
analyzer away the same kind of request still publishes and says so.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import anyio
import pytest

from shijhon.audiomuse import AudioMuse
from shijhon.audiomuse import sync as sync_module
from shijhon.playlists.model import Connection
from tests.acceptance.test_agent_song_facts import agent, ask
from tests.acceptance.test_R_playlists import wait_state
from tests.conftest import NavidromeFactory
from tests.harness.agents import connect_agent
from tests.harness.collection import refresh_collection
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_audiomuse import FakeAudioMuse, FakeSong
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

TOKEN = "musetoken-3d90b1c2e7"  # invented
SERVER = "Shijhon"
REFERENCE = "Velvet Orbit"
SEARCH = "driving fast drums energetic"
DESCRIPTION = {"text": SEARCH, "more": [], "less": []}
API = "/shijhon/api/playlists/requests"

# Calm songs (the reference), then the rest: where they lie decides what sounds alike.
SONGS: dict[str, tuple[tuple[float, float], float]] = {
    # title: (place, energy)
    "Slow Lamp": ((0.0, 0.0), 0.10),
    "Night Kettle": ((0.2, 0.0), 0.14),
    "Soft Harbor": ((0.4, 0.0), 0.18),
    "Gentle Field": ((0.6, 0.1), 0.20),
    "Plain Song": ((6.0, 5.0), 0.50),
    "Hard Run": ((8.0, 7.0), 0.82),
    "Rise Up": ((9.0, 9.0), 0.88),
    "Fast Door": ((10.0, 10.0), 0.92),
    "Loud Window": ((10.5, 10.0), 0.95),
}
REFERENCE_SONGS = ["Slow Lamp", "Night Kettle", "Soft Harbor"]
ALBUMS = (
    Album(
        "Aster & Vale",
        "Orbit Sessions",
        tuple(Track(t, n) for n, t in enumerate(REFERENCE_SONGS, 1)),
    ),
    Album("Tin Harbor", "Quiet Fields", (Track("Gentle Field", 1),)),
    Album(
        "Inkwell & Reed",
        "Bright Rooms",
        (Track("Loud Window", 1), Track("Fast Door", 2), Track("Plain Song", 3)),
    ),
    Album("Moss Atlas", "Lift Cycle", (Track("Rise Up", 1), Track("Hard Run", 2))),
)
ARTISTS = {t: a.artist for a in ALBUMS for t in (x.title for x in a.tracks)}


@pytest.fixture(scope="module")
def muse() -> Iterator[FakeAudioMuse]:
    fake = FakeAudioMuse(TOKEN, servers={SERVER: "srv-1"}, default=SERVER)
    fake.start()
    yield fake
    fake.stop()


class Music:
    """The world, the invented songs' Navidrome IDs and the reference playlist's."""

    def __init__(self, world: DeliveryWorld, browser: Browser, ids: dict[str, str], ref: str):
        self.world, self.browser, self.ids, self.reference = world, browser, ids, ref

    @property
    def titles(self) -> dict[str, str]:
        return {ident: title for title, ident in self.ids.items()}


@pytest.fixture(scope="module")
def music(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    muse: FakeAudioMuse,
) -> Iterator[Music]:
    nd = navidrome_factory()
    for album in ALBUMS:
        write_album(nd.music, album)
    nd.scan()
    patch = pytest.MonkeyPatch()
    patch.setattr(sync_module, "START_DELAY", 3600.0)  # (the test reads the analysis itself)
    settings = {"url": muse.base_url, "token": TOKEN, "reach": "loopback", "server": SERVER}
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("music-facts"),
        agents=True,
        audiomuse={**settings, "timeout_seconds": 5, "sync_hours": 1},
    ) as w:
        browser = Browser(w.server.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        # Sign-in queues source preparation; model calls must not race that work.
        refresh_collection(w.server, str(nd.login()["id"]))
        admin = nd.client()  # (the IDs are Navidrome's; the app reads them there too)
        found = admin.ok("search3", [("query", ""), ("songCount", "100")])
        ids = {s["title"]: s["id"] for s in found["searchResult3"]["song"]}
        assert set(ids) == set(SONGS)
        # The analyzer has analyzed all of them, under Navidrome's IDs.
        muse.add(
            *(
                FakeSong(
                    ids={SERVER: ids[title]},
                    title=title,
                    artist=ARTISTS[title],
                    vector=place,
                    tempo=70.0 + energy * 100,
                    energy=energy,
                    features={
                        "danceable": energy,
                        "aggressive": energy * 0.8,
                        "happy": 0.5,
                        "party": energy * 0.9,
                        "relaxed": 1 - energy,
                        "sad": 0.6 - energy / 2,
                    },
                )
                for title, (place, energy) in SONGS.items()
            )
        )
        sound = w.services.playlists.sound if w.services.playlists else None
        assert isinstance(sound, AudioMuse), "the app connects to the analyzer from its settings"
        assert w.server.call(sound.sync.sync).fetched == len(SONGS)
        created = admin.ok(
            "createPlaylist",
            [("name", REFERENCE), *(("songId", ids[t]) for t in REFERENCE_SONGS)],
        )
        assert (
            browser.submit(
                "playlists",
                {"password": ADMIN_PASSWORD, "history_user": "", "backend": "none"},
                "playlists/connection",
            ).status_code
            == 303
        )
        assert (
            browser.submit(
                "playlists",
                {"backend": "api", "endpoint": "https://model.invalid/v1", "model": "synthetic"},
                "playlists/connection",
            ).status_code
            == 303
        )
        yield Music(w, browser, ids, str(created["playlist"]["id"]))
        browser.close()
    patch.undo()


class Model:
    """A scripted model: records every exchange, answers with the schemas it is given."""

    def __init__(self, music: Music, answers: dict[str, Any]) -> None:
        self.music, self.answers = music, answers
        self.exchanges: list[tuple[type, dict[str, Any]]] = []

    def handles(self, payload: dict[str, Any]) -> dict[str, str]:
        return {c["title"]: c["id"] for c in payload.get("candidates", [])}

    async def __call__(
        self, connection: Connection, prompt: str, *, schema: type, **kwargs: Any
    ) -> Any:
        payload = json.loads(prompt)
        self.exchanges.append((schema, payload))
        answer = self.answers[schema.__name__]
        return answer(payload) if callable(answer) else answer

    def asked(self, name: str) -> list[dict[str, Any]]:
        return [p for s, p in self.exchanges if s.__name__ == name]


def pick(handles: dict[str, str], title: str, fit: str, reason: str) -> dict[str, str]:
    return {"id": handles[title], "reason": reason, "fit": fit}


def install(music: Music, model: Model) -> None:
    service = music.world.services.playlists
    assert service

    async def put() -> None:
        service.curator.api = model

    music.world.server.call(put)


def token(music: Music, scopes: str = "create status library") -> str:
    return str(
        connect_agent(
            music.browser.http,
            str(music.browser.http.base_url).rstrip("/"),
            ADMIN_USER,
            ADMIN_PASSWORD,
            scopes,
        )["access_token"]
    )


def submit(music: Music, key: str, payload: dict[str, Any]) -> tuple[dict[str, str], str, str]:
    auth = {"Authorization": "Bearer " + token(music), "Idempotency-Key": key}
    response = music.browser.http.post(API, headers=auth, json=payload)
    assert response.status_code == 202, response.text
    return auth, response.json()["id"], response.json()["run"]


def native_titles(music: Music, playlist_id: str) -> list[str]:
    entries = music.world.client().ok("getPlaylist", [("id", playlist_id)])["playlist"]["entry"]
    return [e["title"] for e in entries]


def test_a_request_like_a_playlist_but_livelier_uses_sound_and_publishes(
    music: Music, muse: FakeAudioMuse
) -> None:
    # The analyzer ranks by where songs lie and answers the description with its own list.
    muse.search_results[SEARCH] = [music.ids[t] for t in ("Hard Run", "Rise Up", "Loud Window")]
    muse.clear()

    def propose(payload: dict[str, Any]) -> dict[str, Any]:
        tracks = [  # (the model also names a song of the reference, which is left out)
            {"artist": ARTISTS[t], "title": t, "album": "", "version": ""}
            for t in ("Loud Window", "Fast Door", "Slow Lamp")
        ]
        search = {"text": SEARCH, "more": ["drums"], "less": ["acoustic"]}
        return {"tracks": tracks, "sound_search": search}

    def order(payload: dict[str, Any]) -> dict[str, Any]:
        h = model.handles(payload)
        return {
            "goal": "Livelier songs than the Velvet Orbit ones, one of them still too soft",
            "constraints_satisfied": True,
            "selections": [
                pick(h, "Loud Window", "meets", "Fast and bright, well above the reference"),
                pick(h, "Rise Up", "meets", "Driving, found by its sound"),
                pick(h, "Gentle Field", "misses", "Sounds like the reference, not livelier"),
            ],
        }

    def revise(payload: dict[str, Any]) -> dict[str, Any]:
        h = {c["title"]: c["id"] for c in payload["unused_candidates"]}
        h |= {c["title"]: c["id"] for c in payload["previous_selection"]}
        return {
            "goal": "Livelier throughout: the soft song gave way to a harder one",
            "constraints_satisfied": True,
            "selections": [
                pick(h, "Loud Window", "meets", "Fast and bright"),
                pick(h, "Rise Up", "meets", "Driving"),
                pick(h, "Hard Run", "meets", "Hard-hitting replacement"),
            ],
        }

    # PROPOSE and ORDER use different schemas; the revision pass shares ORDER's.
    calls = {"order": 0}

    def ordering(payload: dict[str, Any]) -> dict[str, Any]:
        calls["order"] += 1
        return (order if "previous_selection" not in payload else revise)(payload)

    model = Model(music, {"SoundProposals": propose, "Ordered": ordering})
    install(music, model)
    auth, ident, run = submit(
        music,
        "like-orbit-livelier",
        {
            "name": "Livelier orbit",
            "prompt": f"Like my {REFERENCE} playlist but more energetic, with drive",
            "count": 3,
            "max_per_artist": 2,
            "use_library": True,
            "reference_playlists": [music.reference],
        },
    )
    done = wait_state(music.world, run, ("published", "failed", "uncertain"))
    assert done["state"] == "published", done["error"]

    # What the model was asked: the sound-aware proposal schema, the reference songs with
    # their sound facts and a profile, and the instruction to relate to it.
    schemas = [s.__name__ for s, _ in model.exchanges]
    assert schemas == ["SoundProposals", "Ordered", "Ordered"]  # propose, order, revise
    propose_payload = model.asked("SoundProposals")[0]
    reference = propose_payload["reference"]
    assert reference["playlists"] == [{"name": REFERENCE, "detected": False}]
    assert reference["leave_out_reference_songs"] is True
    assert {s["title"] for s in reference["songs"]} == set(REFERENCE_SONGS)
    for song in reference["songs"]:
        assert song["source"] == "own" and song["sound"]["energy"] <= 40, song
        assert song["sound"]["tempo"] and song["year"] == 2020
    profile = reference["profile"]
    assert profile["songs"] == 3 and profile["with_sound"] == 3
    assert profile["energy"] <= 40 and profile["features"]["relaxed"] >= 60
    assert propose_payload["instructions"].startswith("Like my Velvet Orbit")

    first, second = model.asked("Ordered")
    candidates = {c["title"]: c for c in first["candidates"]}
    # The model's own picks, and the library songs sound analysis added, labelled.
    assert set(candidates) == {
        "Loud Window",
        "Fast Door",
        "Gentle Field",
        "Plain Song",
        "Hard Run",
        "Rise Up",
    }
    assert not set(REFERENCE_SONGS) & set(candidates)  # left out, though the model named one
    assert "suggested_by" not in candidates["Loud Window"]
    assert candidates["Gentle Field"]["suggested_by"] == "sound_similar"
    assert candidates["Hard Run"]["suggested_by"] == "sound_similar"
    assert candidates["Rise Up"]["suggested_by"] == "sound_search"
    for title, c in candidates.items():
        assert c["source"] == "own" and c["sound"]["energy"] is not None, title
    assert candidates["Loud Window"]["sound"]["energy"] > 90
    assert first["reference"]["profile"] == profile
    # The revision pass saw the flagged song, the first goal and what was left over.
    assert second["flagged"] == [candidates["Gentle Field"]["id"]]
    assert second["previous_goal"].startswith("Livelier songs than")
    assert {c["title"] for c in second["unused_candidates"]} == {
        "Fast Door",
        "Plain Song",
        "Hard Run",
    }

    # The analyzer was asked for songs like the reference songs, and for the description.
    alchemy = muse.requests("alchemy")[0]["body"]
    assert [i["id"] for i in alchemy["items"]] == [music.ids[t] for t in REFERENCE_SONGS]
    search = muse.requests("search")[0]["body"]
    assert search["query"] == SEARCH and search["steering"]
    assert {r["authorization"] for r in muse.requests() if r["endpoint"] != "health"} == {
        f"Bearer {TOKEN}"
    }

    # Navidrome holds the playlist with the revised selection, in that order.
    status = music.browser.http.get(f"{API}/{ident}", headers=auth).json()
    assert status["state"] == "published", status
    assert native_titles(music, status["playlist_id"]) == ["Loud Window", "Rise Up", "Hard Run"]

    # ... and the request tells how it was made.
    assert status["goal"].startswith("Livelier throughout")
    assert status["revision"] == "revised"
    assert status["references"] == {
        "playlists": [{"id": music.reference, "name": REFERENCE, "songs": 3, "detected": False}],
        "songs": 0,
        "shown": 3,
        "exclude": True,
    }
    assert status["reference_profile"] == profile
    assert [(s["title"], s["fit"]) for s in status["selected"]] == [
        ("Loud Window", "meets"),
        ("Rise Up", "meets"),
        ("Hard Run", "meets"),
    ]
    by_title = {s["title"]: s for s in status["selected"]}
    assert by_title["Rise Up"]["facts"]["suggested_by"] == "sound_search"
    assert by_title["Hard Run"]["facts"]["suggested_by"] == "sound_similar"
    assert by_title["Hard Run"]["reason"] == "Hard-hitting replacement"
    assert all(
        s["source"] == "own" and s["facts"]["sound"]["energy"] > 55 for s in by_title.values()
    )
    assert status["coverage"] == {
        "songs": 3,
        "with_sound": 3,
        "with_tags": 0,
        "sound": "used",
        "tags": "off",
    }
    assert status["sound_pool"] == {"similar": 3, "search": 1}
    assert status["left_out"] == 1


def test_an_agent_sees_the_same_facts_through_the_connected_analyzer(
    music: Music, muse: FakeAudioMuse
) -> None:
    w = music.world
    owner = connect_agent(
        music.browser.http,
        str(music.browser.http.base_url).rstrip("/"),
        ADMIN_USER,
        ADMIN_PASSWORD,
        "create status library",
        client_name="Facts agent",
    )

    # Scripted here too: under parallel runs this test may not follow the first one.
    muse.search_results[SEARCH] = [music.ids[t] for t in ("Hard Run", "Rise Up", "Loud Window")]

    async def run() -> None:
        async with agent(w, owner["access_token"]) as session:
            capabilities = (await ask(session, "get_capabilities", {}))["features"]
            sound = capabilities["sound_analysis"]
            assert sound["configured"] and sound["reachable"] and sound["ready"]
            assert sound["analyzed_songs"] == len(SONGS)
            hard = music.ids["Hard Run"]
            facts = await ask(session, "get_song_facts", {"song_ids": [hard]})
            assert facts["sound_analysis"] == "ok" and facts["tags"] == "not_configured"
            assert facts["songs"][0]["sound"]["energy"] > 60
            profile = await ask(session, "get_playlist_profile", {"playlist_id": music.reference})
            assert profile["summary"]["with_sound_facts"] == 3
            assert profile["summary"]["sound"]["energy"]["median"] <= 40
            similar = await ask(
                session, "find_similar_songs", {"playlist_id": music.reference, "count": 2}
            )
            # (not the playlist's own songs; the first by where they lie)
            assert [s["title"] for s in similar["songs"]] == ["Gentle Field", "Plain Song"]
            assert all("sound" in s for s in similar["songs"])
            sounds = await ask(session, "search_by_sound", {"text": SEARCH, "count": 2})
            assert [s["title"] for s in sounds["songs"]] == ["Hard Run", "Rise Up"]

    anyio.run(run)


def test_with_the_analyzer_away_the_playlist_still_publishes_and_says_so(
    music: Music, muse: FakeAudioMuse
) -> None:
    muse.statuses.update(dict.fromkeys(("similar_tracks", "alchemy", "search"), 503))
    muse.clear()
    try:

        def propose(payload: dict[str, Any]) -> dict[str, Any]:
            tracks = [
                {"artist": ARTISTS[t], "title": t, "album": "", "version": ""}
                for t in ("Night Kettle", "Loud Window", "Fast Door")
            ]
            return {"tracks": tracks, "sound_search": DESCRIPTION}

        def order(payload: dict[str, Any]) -> dict[str, Any]:
            if "previous_selection" in payload:  # the revision: an unusable answer
                return {
                    "name": "x",
                    "goal": "x",
                    "constraints_satisfied": False,
                    "selections": [pick(model.handles(payload), "Fast Door", "meets", "x")],
                }
            h = model.handles(payload)
            return {
                "name": "Livelier orbit, by ear",
                "goal": "Two bright songs and one reference song, chosen without sound",
                "constraints_satisfied": True,
                "selections": [
                    pick(h, "Loud Window", "meets", "Bright"),
                    pick(h, "Night Kettle", "partly", "A reference song"),
                ],
            }

        model = Model(music, {"SoundProposals": propose, "NamedOrdered": order})
        install(music, model)
        # The playlist is named in the instructions; the user chose no reference.
        auth, ident, run = submit(
            music,
            "like-orbit-away",
            {
                "name": "",
                "prompt": f"A livelier take on my {REFERENCE} playlist",
                "count": 2,
                "max_per_artist": 2,
                "use_library": True,
            },
        )
        done = wait_state(music.world, run, ("published", "failed", "uncertain"))
        assert done["state"] == "published", done["error"]
        status = music.browser.http.get(f"{API}/{ident}", headers=auth).json()
    finally:
        muse.statuses.clear()
    assert status["name"] == "Livelier orbit, by ear"
    assert native_titles(music, status["playlist_id"]) == ["Loud Window", "Night Kettle"]
    assert status["references"]["playlists"] == [
        {"id": music.reference, "name": REFERENCE, "songs": 3, "detected": True}
    ]
    assert status["coverage"]["sound"] == "unavailable" and status["coverage"]["tags"] == "off"
    assert status["sound_pool"] == {"similar": 0, "search": 0}
    # A playlist only named in the instructions does not remove its songs by itself.
    assert status["left_out"] == 0
    # A revision that cannot be used leaves the first selection as it was.
    assert [s.__name__ for s, _ in model.exchanges] == [
        "SoundProposals",
        "NamedOrdered",
        "NamedOrdered",
    ]
    assert status["revision"] == "kept"
    assert [s["fit"] for s in status["selected"]] == ["meets", "partly"]
