"""AudioMuse-AI's analysis kept in ``sound_facts`` and used for sound-based suggestions, against
a fake AudioMuse-AI with the shapes of its source (``tests/harness/fake_audiomuse.py``)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.audiomuse import AudioMuse, AudioMuseClient
from shijhon.audiomuse import client as api
from shijhon.delivery.netpolicy import Reach
from shijhon.playlists.sound import FEATURES, SoundUnavailable
from shijhon.store import Store
from tests.harness.fake_audiomuse import FakeAudioMuse, FakeSong

TOKEN = "tok-7d41c0a95e2b38f6"  # invented
SERVER = "Shijhon"


def scored(offset: float) -> dict[str, float]:
    return {name: round(0.4 + offset + i * 0.01, 2) for i, name in enumerate(FEATURES)}


def song(n: int, **more: Any) -> FakeSong:
    """Songs 1..n lie on a line, louder and livelier with n; their IDs differ per server."""
    base: dict[str, Any] = {
        "ids": {SERVER: f"nd-{n}", "Other": f"jf-{n}"},
        "title": f"Title {n}",
        "artist": f"Artist {n % 4}",
        "vector": (float(n), 0.0),
        "tempo": 100.0 + n,
        "energy": round(0.1 + n * 0.05, 2),
        "features": scored(n * 0.02),
        "moods": {"rock": 0.7, "indie": 0.2},
    }
    return FakeSong(**{**base, **more})


@pytest.fixture
def fake() -> Iterator[FakeAudioMuse]:
    server = FakeAudioMuse(TOKEN, servers={SERVER: "srv-1", "Other": "srv-2"}, default=SERVER)
    server.add(*(song(n) for n in range(1, 11)))
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Setup:
    def __init__(self, store: Store, fake: FakeAudioMuse, sound: AudioMuse) -> None:
        self.store, self.fake, self.sound = store, fake, sound

    @property
    def sync(self) -> Any:
        return self.sound.sync


async def connect(
    fake: FakeAudioMuse, store: Store, *, server: str = SERVER, patience: float = 5.0, **more: Any
) -> AudioMuse:
    client = AudioMuseClient(
        fake.base_url, TOKEN, server=server, reach=Reach.LOOPBACK, timeout=patience
    )
    return AudioMuse(client, store, sync_hours=6, **more)


@pytest.fixture
async def setup(fake: FakeAudioMuse, tmp_path: Path) -> AsyncIterator[Setup]:
    store = await Store.open(tmp_path / "state.sqlite3")
    sound = await connect(fake, store)
    try:
        yield Setup(store, fake, sound)
    finally:
        await sound.aclose()
        await store.close()


async def until(done: Callable[[], Awaitable[bool] | bool]) -> None:
    """Wait (at most five seconds) for the background work."""
    with anyio.fail_after(5):
        while True:
            result = done()
            if (await result) if isinstance(result, Awaitable) else result:
                return
            await anyio.sleep(0.02)


def sync_requests(fake: FakeAudioMuse) -> list[dict[str, Any]]:
    return fake.requests("sync")


async def stored_count(store: Store, at_least: int) -> bool:
    return len(await stored(store)) >= at_least


async def stored(store: Store) -> set[str]:
    rows = await store.fetchall("SELECT song_id FROM sound_facts")
    return {row["song_id"] for row in rows}


# --- the sync ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_sync_saves_the_raw_facts_by_navidrome_song_id(setup: Setup) -> None:
    report = await setup.sync.sync()
    assert (report.listed, report.fetched, report.removed) == (10, 10, 0)
    assert await stored(setup.store) == {f"nd-{n}" for n in range(1, 11)}  # not the other server's
    row = await setup.store.fetchone("SELECT * FROM sound_facts WHERE song_id = 'nd-3'")
    assert row is not None
    assert row["tempo"] == 103.0 and row["musical_key"] == "A minor" and row["energy"] == 0.25
    assert json.loads(row["labels"]) == ["rock", "indie"]
    assert json.loads(row["features"]) == scored(0.06)  # raw, not percentiles
    assert row["item_id"] == setup.fake.songs[2].fingerprint() and row["synced"] > 0
    # Every request carried the token and the server; the lists were asked for compactly.
    assert {r["authorization"] for r in setup.fake.requests() if r["endpoint"] != "health"} == {
        f"Bearer {TOKEN}"
    }
    assert [r["params"].get("server") for r in sync_requests(setup.fake)] == [SERVER] * 2
    assert sync_requests(setup.fake)[0]["params"]["fields"] == "index"
    assert sync_requests(setup.fake)[1]["params"]["include_embeddings"] == "false"


@pytest.mark.anyio
async def test_the_second_sync_asks_for_the_list_only(setup: Setup) -> None:
    await setup.sync.sync()
    setup.fake.clear()
    report = await setup.sync.sync()
    assert (report.fetched, report.removed) == (0, 0)
    assert [r["params"].get("fields") for r in sync_requests(setup.fake)] == ["index"]
    first = await setup.store.fetchone("SELECT MAX(synced) AS at FROM sound_facts")
    assert first is not None and first["at"] > time.time() - 60  # confirmed again


@pytest.mark.anyio
async def test_only_new_and_changed_songs_are_fetched_again(setup: Setup) -> None:
    await setup.sync.sync()
    setup.fake.songs[1].revision += 1  # analyzed again
    setup.fake.songs[1].energy = 0.9
    setup.fake.add(song(11))
    setup.fake.clear()
    report = await setup.sync.sync()
    assert (report.listed, report.fetched, report.removed) == (11, 2, 0)
    asked = [r["params"] for r in sync_requests(setup.fake) if "ids" in r["params"]]
    assert len(asked) == 1 and set(asked[0]["ids"].split(",")) == {"nd-2", "nd-11"}
    facts = await setup.sound.facts(["nd-2"])
    assert facts["nd-2"].energy is not None and facts["nd-2"].energy >= 90  # the new value


@pytest.mark.anyio
async def test_a_big_library_is_fetched_in_groups_two_at_a_time(
    fake: FakeAudioMuse, tmp_path: Path
) -> None:
    fake.songs.clear()
    fake.add(*(song(n, ids={SERVER: f"nd-{n:05d}x" + "k" * 12}) for n in range(1, 1251)))
    fake.delays["sync"] = 0.05
    store = await Store.open(tmp_path / "state.sqlite3")
    sound = await connect(fake, store)
    try:
        report = await sound.sync.sync()
        assert report.fetched == 1250 and await store.fetchone("SELECT 1 FROM sound_facts")
        assert len(await stored(store)) == 1250
        pages = [r for r in sync_requests(fake) if r["params"].get("fields") == "index"]
        assert [r["params"]["page"] for r in pages] == ["1", "2"]  # 1000 a page
        groups = [
            r["params"]["ids"].split(",") for r in sync_requests(fake) if "ids" in r["params"]
        ]
        assert len(groups) == 13 and max(len(g) for g in groups) == 100  # 100 a request
        assert 1 < fake.most_at_once <= api.MAX_AT_ONCE  # in parallel, leaving room for playlists
    finally:
        await sound.aclose()
        await store.close()


@pytest.mark.anyio
async def test_songs_audiomuse_no_longer_lists_are_removed(setup: Setup) -> None:
    await setup.sync.sync()
    gone = setup.fake.songs.pop(0)
    setup.fake.clear()
    report = await setup.sync.sync()
    assert report.removed == 1
    assert gone.ids[SERVER] not in await stored(setup.store)
    assert len(await stored(setup.store)) == 9
    # (Asked for once more before it was removed: it was not there.)
    assert [r["params"]["ids"] for r in sync_requests(setup.fake) if "ids" in r["params"]] == [
        gone.ids[SERVER]
    ]


@pytest.mark.anyio
async def test_a_song_missing_from_the_list_but_still_there_is_kept(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A song analyzed while the list was read page by page can fall between pages.
    await setup.sync.sync()
    real = setup.sound.client.manifest

    async def without_nd_4(page: int) -> tuple[list[tuple[str, str]], bool]:
        rows, more = await real(page)
        return [row for row in rows if row[0] != "nd-4"], more

    monkeypatch.setattr(setup.sound.client, "manifest", without_nd_4)
    report = await setup.sync.sync()
    assert report.removed == 0 and "nd-4" in await stored(setup.store)


@pytest.mark.anyio
async def test_a_list_audiomuse_empties_removes_what_it_no_longer_has(setup: Setup) -> None:
    await setup.sync.sync()
    setup.fake.songs.clear()
    report = await setup.sync.sync()
    assert report.removed == 10 and await stored(setup.store) == set()


@pytest.mark.anyio
async def test_silent_songs_and_placeholders_have_no_facts(setup: Setup) -> None:
    setup.fake.songs[0].energy = 0.0  # an analyzed silent file
    await setup.store.execute(
        "INSERT INTO releases (ref, folder, album_id, title, artist, album_tags, data,"
        " created_at) VALUES ('c:1', '_s/1', 'al', 'T', 'A', '{}', '{}', 0)"
    )
    for song_id, state in (("nd-2", "placeholder"), ("nd-3", "delivered")):
        await setup.store.execute(
            "INSERT INTO placeholders (song_id, path, placeholder_path, track_ref, release_ref,"
            " title, artist, album, duration_ms, disc, track, tags, state, created_at)"
            " VALUES (?, ?, ?, ?, 'c:1', 'T', 'A', 'B', 1000, 1, 1, '{}', ?, 0)",
            [song_id, f"p/{song_id}", f"q/{song_id}", f"t-{song_id}", state],
        )
    report = await setup.sync.sync()
    # nd-1 is silent, nd-2 a placeholder (not even fetched); nd-3 was delivered: real audio.
    assert await stored(setup.store) == {f"nd-{n}" for n in range(3, 11)}
    assert report.fetched == 8
    fetched = {
        i for r in sync_requests(setup.fake) for i in r["params"].get("ids", "").split(",") if i
    }
    assert "nd-2" not in fetched
    # A song that becomes a placeholder again loses its facts at the next sync.
    await setup.store.execute(
        "UPDATE placeholders SET state = 'placeholder' WHERE song_id = 'nd-3'"
    )
    assert (await setup.sync.sync()).removed == 1
    assert "nd-3" not in await stored(setup.store)


@pytest.mark.anyio
async def test_songs_the_library_has_lost_are_removed(setup: Setup) -> None:
    await setup.sync.sync()
    for song_id, present in (("nd-5", 0), ("nd-6", 1)):
        await setup.store.execute(
            "INSERT INTO collection_tracks (library_id, track_key, song_id, release_key,"
            " album_id, artist_id, title, album, artist, search_text, original, present,"
            " generation, indexed_at) VALUES ('1', ?, ?, 'r', 'al', 'ar', 'T', 'A', 'B', '',"
            " 1, ?, 'g', 0)",
            [f"k-{song_id}", song_id, present],
        )
    report = await setup.sync.sync()  # (nd-5 is gone from the library; AudioMuse-AI still lists it)
    assert report.removed == 1
    assert await stored(setup.store) == {f"nd-{n}" for n in range(1, 11)} - {"nd-5"}


@pytest.mark.anyio
async def test_a_failed_sync_keeps_what_was_saved_and_says_why(setup: Setup) -> None:
    await setup.sync.sync()
    setup.fake.statuses["sync"] = 500
    with pytest.raises(SoundUnavailable):
        await setup.sync.sync()
    assert setup.sync.error == api.FAILED and len(await stored(setup.store)) == 10
    del setup.fake.statuses["sync"]
    await setup.sync.sync()
    assert setup.sync.error == "" and setup.sync.finished > 0


@pytest.mark.anyio
async def test_a_sync_cut_short_continues_where_it_stopped(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup.fake.add(*(song(n) for n in range(11, 261)))  # 260 songs: three groups
    real = setup.sound.client.tracks
    calls = []

    async def failing_third(ids: Any) -> Any:
        calls.append(list(ids))
        if len(calls) == 3:
            raise SoundUnavailable(api.SLOW)
        return await real(ids)

    monkeypatch.setattr(setup.sound.client, "tracks", failing_third)
    with pytest.raises(SoundUnavailable):
        await setup.sync.sync()
    kept = await stored(setup.store)
    assert kept  # the first group was saved before the failure
    monkeypatch.undo()
    setup.fake.clear()
    report = await setup.sync.sync()
    assert len(await stored(setup.store)) == 260 and report.fetched == 260 - len(kept)


@pytest.mark.anyio
async def test_a_sync_is_not_started_twice(setup: Setup) -> None:
    setup.fake.delays["sync"] = 0.2
    async with anyio.create_task_group() as tasks:
        results: list[Any] = []

        async def one() -> None:
            results.append(await setup.sync.sync())

        tasks.start_soon(one)
        await anyio.sleep(0.05)
        tasks.start_soon(one)
    assert sorted(r.listed for r in results) == [0, 10]  # the second waited and did nothing
    assert len([r for r in sync_requests(setup.fake) if r["params"].get("fields")]) == 1


# --- when it runs --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_it_syncs_after_startup_when_the_facts_are_old_and_then_every_interval(
    setup: Setup,
) -> None:
    setup.sync.start_delay = 0.0
    setup.sync.interval = 0.4
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(setup.sound.run)
        await until(lambda: stored_count(setup.store, 10))
        first = len(sync_requests(setup.fake))
        await anyio.sleep(0.15)
        assert len(sync_requests(setup.fake)) == first  # not again before the interval
        await until(lambda: len(sync_requests(setup.fake)) > first)
        tasks.cancel_scope.cancel()  # stops cleanly
    assert setup.sync.error == ""


@pytest.mark.anyio
async def test_fresh_facts_are_not_fetched_again_at_startup(setup: Setup) -> None:
    await setup.sync.sync()
    setup.fake.clear()
    setup.sync.start_delay = 0.0
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(setup.sound.run)
        await anyio.sleep(0.3)
        tasks.cancel_scope.cancel()
    assert sync_requests(setup.fake) == []


@pytest.mark.anyio
async def test_a_failed_sync_is_tried_again_soon(setup: Setup) -> None:
    setup.fake.statuses["sync"] = 503
    setup.sync.start_delay, setup.sync.retry_seconds = 0.0, 0.1
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(setup.sound.run)
        await until(lambda: setup.sync.error == api.NOT_READY)
        del setup.fake.statuses["sync"]

        async def recovered() -> bool:
            # Songs are saved per group before the run ends and clears its error.
            return setup.sync.error == "" and await stored_count(setup.store, 10)

        await until(recovered)
        tasks.cancel_scope.cancel()
    assert setup.sync.error == ""


# --- facts ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_facts_are_percentiles_among_the_synced_songs(setup: Setup) -> None:
    await setup.sync.sync()
    facts = await setup.sound.facts(["nd-1", "nd-5", "nd-10", "nd-404"])
    assert set(facts) == {"nd-1", "nd-5", "nd-10"}  # the unknown has none
    assert facts["nd-1"].energy == 5 and facts["nd-5"].energy == 45 and facts["nd-10"].energy == 95
    assert facts["nd-5"].tempo == 105.0 and facts["nd-5"].key == "A minor"
    assert facts["nd-5"].labels == ["rock", "indie"]
    assert set(facts["nd-5"].features) == set(FEATURES)
    assert facts["nd-1"].features["happy"] < facts["nd-10"].features["happy"]
    assert all(0 <= v <= 100 for f in facts.values() for v in f.features.values())


@pytest.mark.anyio
async def test_percentiles_follow_the_library_after_a_sync(setup: Setup) -> None:
    await setup.sync.sync()
    assert (await setup.sound.facts(["nd-5"]))["nd-5"].energy == 45
    setup.fake.add(*(song(n, energy=0.01) for n in range(11, 31)))  # twenty quiet songs
    await setup.sync.sync()
    assert (await setup.sound.facts(["nd-5"]))["nd-5"].energy == 82  # louder than most now


@pytest.mark.anyio
async def test_songs_not_scored_yet_have_the_rest_but_no_features(setup: Setup) -> None:
    setup.fake.songs[0].features = None  # its CLAP step has not run: all 0.00
    await setup.sync.sync()
    facts = await setup.sound.facts(["nd-1", "nd-2"])
    assert facts["nd-1"].features == {} and facts["nd-1"].energy is not None
    assert len(facts["nd-2"].features) == 6
    assert facts["nd-2"].features["happy"] in range(0, 101)
    # (The unscored song does not count among those the others are placed among.)
    assert facts["nd-2"].features["happy"] < 20


# --- similar, search, path -----------------------------------------------------------------


@pytest.mark.anyio
async def test_similar_to_one_song_uses_its_neighbours(setup: Setup) -> None:
    await setup.sync.sync()
    found = await setup.sound.similar(["nd-5"], count=4)
    assert found == ["nd-4", "nd-6", "nd-3", "nd-7"]  # nearest first, the seed left out
    asked = setup.fake.requests("similar_tracks")[0]["params"]
    assert asked["item_id"] == "nd-5" and asked["server"] == SERVER
    assert not setup.fake.requests("alchemy")


@pytest.mark.anyio
async def test_similar_to_several_songs_blends_them_and_avoids_others(setup: Setup) -> None:
    await setup.sync.sync()
    found = await setup.sound.similar(["nd-2", "nd-4"], avoid=["nd-8"], count=3)
    assert len(found) == 3 and "nd-2" not in found and "nd-4" not in found
    assert found[0] == "nd-3" or found[0] in ("nd-1", "nd-5")  # around the centre, nearest first
    body = setup.fake.requests("alchemy")[0]["body"]
    assert body["server"] == SERVER and body["temperature"] == 0  # a ranking, not a sample
    assert body["items"] == [
        {"id": "nd-2", "op": "ADD", "type": "song"},
        {"id": "nd-4", "op": "ADD", "type": "song"},
        {"id": "nd-8", "op": "SUBTRACT", "type": "song"},
    ]
    # One song to avoid alone is not a blend: it takes the blend too.
    await setup.sound.similar(["nd-2"], avoid=["nd-9"])
    assert len(setup.fake.requests("alchemy")) == 2


@pytest.mark.anyio
async def test_results_are_navidrome_ids_of_songs_with_facts_in_audiomuses_order(
    setup: Setup,
) -> None:
    await setup.sync.sync()
    # A song AudioMuse-AI has analyzed since the sync is not ours yet: left out.
    setup.fake.add(song(11, vector=(5.1, 0.0)))
    found = await setup.sound.similar(["nd-5"], count=3)
    assert found == ["nd-4", "nd-6", "nd-3"] and "nd-11" not in found
    assert all(ident.startswith("nd-") for ident in found)  # never "jf-…" or "fp_…"
    await setup.sync.sync()
    assert (await setup.sound.similar(["nd-5"], count=3))[0] == "nd-11"


@pytest.mark.anyio
async def test_ids_are_the_configured_servers(fake: FakeAudioMuse, tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    other = await connect(fake, store, server="srv-2")  # by ID; the other server's songs
    try:
        await other.sync.sync()
        assert await stored(store) == {f"jf-{n}" for n in range(1, 11)}
        assert await other.similar(["jf-5"], count=2) == ["jf-4", "jf-6"]
    finally:
        await other.aclose()
        await store.close()


@pytest.mark.anyio
async def test_a_song_the_server_lacks_is_left_out(setup: Setup) -> None:
    setup.fake.songs[5].ids.pop(SERVER)  # nd-6 is not on Shijhon's server
    await setup.sync.sync()
    assert "nd-6" not in await stored(setup.store)
    assert await setup.sound.similar(["nd-5"], count=3) == ["nd-4", "nd-3", "nd-7"]


@pytest.mark.anyio
async def test_similar_asks_for_more_than_it_returns_and_returns_what_was_asked(
    setup: Setup,
) -> None:
    await setup.sync.sync()
    assert len(await setup.sound.similar(["nd-1"], count=2)) == 2
    assert await setup.sound.similar(["nd-1"], count=0) == []
    assert await setup.sound.similar([], count=5) == []
    assert await setup.sound.similar(["unknown-song"], count=5) == []  # (404: none)
    asked = int(setup.fake.requests("similar_tracks")[0]["params"]["n"])
    assert 2 < asked <= api.MAX_RESULTS


@pytest.mark.anyio
async def test_similar_takes_the_largest_asks_its_callers_make_and_any_count(setup: Setup) -> None:
    """Curation asks for the requested count (up to 100) from up to 50 reference songs; the
    agent tools for min(count + playlist size, 100) from up to 20 seeds and 30 playlist
    songs, avoiding up to 20. Whatever the count, the answer is a list, never an error."""
    await setup.sync.sync()
    seeds = ["nd-1", "nd-2", *(f"seed-{n}" for n in range(48))]
    avoid = ["nd-9", *(f"avoid-{n}" for n in range(19))]
    for count in (1, 30, 100, 101, 10_000, 0, -3):
        found = await setup.sound.similar(seeds, avoid=avoid, count=count)
        assert len(found) <= max(0, min(count, 100)), count
        assert not {"nd-1", "nd-2", "nd-9"} & set(found)
        assert all(ident.startswith("nd-") for ident in found)
    assert len(await setup.sound.similar(seeds, avoid=avoid, count=100)) == 7  # all there are
    # More seeds and songs to avoid than it uses are cut, not refused.
    many = [f"nd-{n}" for n in range(1, 11)] * 10
    assert await setup.sound.similar(many, avoid=many, count=100) == []
    sent = [r["body"]["n"] for r in setup.fake.requests("alchemy")]
    assert sent and max(sent) <= api.MAX_RESULTS  # (what its own pages offer at most)


@pytest.mark.anyio
async def test_search_by_description_with_steering(setup: Setup) -> None:
    await setup.sync.sync()
    setup.fake.search_results["warm piano evening"] = ["nd-7", "nd-2", "nd-9", "nd-1"]
    setup.fake.steered[("warm piano evening", (("piano", "more"), ("sad", "less")))] = [
        "nd-9",
        "nd-7",
    ]
    assert await setup.sound.search("warm piano evening", count=3) == ["nd-7", "nd-2", "nd-9"]
    found = await setup.sound.search(
        "Warm  Piano evening", more=["Piano", "bebop"], less=["sad"], count=5
    )
    assert found == ["nd-9", "nd-7"]
    body = setup.fake.requests("search")[1]["body"]
    assert body["steering"] == [
        {"term": "piano", "direction": "more"},
        {"term": "sad", "direction": "less"},
    ]  # "bebop" is no concept: dropped
    assert body["query"] == "Warm Piano evening" and body["server"] == SERVER
    assert len(setup.fake.requests("concepts")) == 1  # fetched once


@pytest.mark.anyio
async def test_a_search_without_the_steering_models_still_works(setup: Setup) -> None:
    await setup.sync.sync()
    setup.fake.available = False
    setup.fake.search_results["calm"] = ["nd-3"]
    assert await setup.sound.search("calm", more=["piano"], count=5) == ["nd-3"]
    assert "steering" not in setup.fake.requests("search")[0]["body"]


@pytest.mark.anyio
async def test_a_search_audiomuse_cannot_do_is_unavailable(setup: Setup) -> None:
    await setup.sync.sync()
    setup.fake.clap = False
    with pytest.raises(SoundUnavailable) as caught:
        await setup.sound.search("calm", count=5)
    assert str(caught.value) == api.REFUSED
    setup.fake.clap, setup.fake.loaded = True, False
    with pytest.raises(SoundUnavailable) as caught:
        await setup.sound.search("calm", count=5)
    assert str(caught.value) == api.NOT_READY


@pytest.mark.anyio
async def test_a_path_goes_from_one_song_to_the_other_with_both_included(setup: Setup) -> None:
    await setup.sync.sync()
    path = await setup.sound.path("nd-1", "nd-9", steps=5)
    assert path[0] == "nd-1" and path[-1] == "nd-9" and len(path) == 5
    assert path == sorted(path, key=lambda s: int(s.split("-")[1]))  # along the line
    asked = setup.fake.requests("find_path")[0]["params"]
    assert (asked["start_song_id"], asked["end_song_id"], asked["server"]) == (
        "nd-1",
        "nd-9",
        SERVER,
    )
    assert asked["max_steps"] == "5"
    assert await setup.sound.path("nd-1", "nd-1") == ["nd-1"]
    assert await setup.sound.path("nd-1", "unknown") == []  # (404: no path)
    assert await setup.sound.path("", "nd-1") == []


# --- failures and time ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_server_that_is_not_there_is_unavailable_at_once(
    fake: FakeAudioMuse, tmp_path: Path
) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sound = await connect(fake, store)
    await sound.sync.sync()
    fake.stop()
    begun = time.monotonic()
    try:
        for call in (
            sound.similar(["nd-5"]),
            sound.search("calm"),
            sound.path("nd-1", "nd-2"),
        ):
            with pytest.raises(SoundUnavailable) as caught:
                await call
            assert str(caught.value) == api.UNREACHABLE
        status = await sound.status()
        assert not status.reachable and status.error == api.UNREACHABLE
        assert status.songs == 10  # (the facts are Shijhon's own: still there)
        assert (await sound.facts(["nd-5"]))["nd-5"].tempo == 105.0
        assert time.monotonic() - begun < 5
    finally:
        await sound.aclose()
        await store.close()


@pytest.mark.anyio
async def test_every_call_is_bounded_in_time(fake: FakeAudioMuse, tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sound = await connect(fake, store, patience=0.3)
    try:
        await sound.sync.sync()
        for endpoint in ("similar_tracks", "alchemy", "search", "find_path", "concepts"):
            fake.delays[endpoint] = 3.0
        begun = time.monotonic()
        for call in (
            sound.similar(["nd-5"]),
            sound.similar(["nd-5", "nd-6"]),
            sound.search("calm", more=["piano"]),
            sound.path("nd-1", "nd-9"),
        ):
            with pytest.raises(SoundUnavailable) as caught:
                await call
            assert str(caught.value) == api.SLOW
        assert time.monotonic() - begun < 3.0  # four calls, each given up after 0.3 s
    finally:
        await sound.aclose()
        await store.close()


@pytest.mark.anyio
async def test_the_wrong_token_is_said_and_the_address_is_checked_by_the_network_setting(
    fake: FakeAudioMuse, tmp_path: Path
) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    wrong = AudioMuse(
        AudioMuseClient(fake.base_url, "not-" + TOKEN, reach=Reach.LOOPBACK, timeout=2),
        store,
        sync_hours=6,
    )
    default_reach = AudioMuse(AudioMuseClient(fake.base_url, TOKEN, timeout=2), store, sync_hours=6)
    try:
        with pytest.raises(SoundUnavailable) as caught:
            await wrong.sync.sync()
        assert str(caught.value) == api.TOKEN_REFUSED and "not-" + TOKEN not in str(caught.value)
        status = await wrong.status()
        assert (status.configured, status.reachable, status.error) == (
            True,
            False,
            api.TOKEN_REFUSED,
        )
        with pytest.raises(SoundUnavailable) as caught:  # loopback is not public
            await default_reach.similar(["nd-1"])
        assert str(caught.value) == api.DENIED
    finally:
        await wrong.aclose()
        await default_reach.aclose()
        await store.close()


@pytest.mark.anyio
async def test_the_status_says_what_there_is(setup: Setup) -> None:
    before = await setup.sound.status()
    assert (before.configured, before.reachable, before.songs, before.synced) == (True, True, 0, 0)
    await setup.sync.sync()
    after = await setup.sound.status()
    assert after.songs == 10 and after.reachable and after.error == ""
    assert after.synced > time.time() - 60


@pytest.mark.anyio
async def test_the_ids_of_a_request_are_checked_not_trusted(setup: Setup) -> None:
    await setup.sync.sync()
    long = "x" * 500
    assert await setup.sound.facts(["", long, "bad\nid"]) == {}
    assert await setup.sound.similar([long, "", "bad\nid"]) == []
    assert not setup.fake.requests("similar_tracks")
    many = [f"nd-{n}" for n in range(1, 11)] * 10
    assert len(await setup.sound.facts(many)) == 10


async def mark_absent(store: Store, song_id: str) -> None:
    await store.execute(
        "INSERT INTO collection_tracks (library_id, track_key, song_id, release_key,"
        " album_id, artist_id, title, album, artist, search_text, original, present,"
        " generation, indexed_at) VALUES ('1', ?, ?, 'r', 'al', 'ar', 'T', 'A', 'B', '',"
        " 1, 0, 'g', 0)",
        [f"k-{song_id}", song_id],
    )


@pytest.mark.anyio
async def test_known_absent_songs_are_not_fetched_or_reinserted(setup: Setup) -> None:
    await mark_absent(setup.store, "nd-5")
    for _ in range(2):
        setup.fake.clear()
        await setup.sync.sync()
        assert "nd-5" not in await stored(setup.store)
        asked = {
            i for r in sync_requests(setup.fake) for i in r["params"].get("ids", "").split(",")
        }
        assert "nd-5" not in asked
    # A song that reappears in the index becomes eligible again.
    await setup.store.execute("UPDATE collection_tracks SET present = 1 WHERE song_id = 'nd-5'")
    await setup.sync.sync()
    assert "nd-5" in await stored(setup.store)


@pytest.mark.anyio
async def test_songs_lost_during_fetch_are_removed_in_the_same_sync(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = setup.sync.table
    put = table.put

    async def save_then_lose(rows: Any, at: float) -> None:
        await put(rows, at)
        await mark_absent(setup.store, "nd-5")

    monkeypatch.setattr(table, "put", save_then_lose)
    report = await setup.sync.sync()
    assert report.fetched == 10 and report.removed == 1
    assert "nd-5" not in await stored(setup.store)
