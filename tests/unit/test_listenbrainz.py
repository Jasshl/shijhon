"""ListenBrainz's songs and artists listened together (a stand-in with invented names), and
how they join Last.fm's: in radio's similarity, in discovery mixes' candidates. Nothing here
reaches a network."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from pydantic import SecretStr

from shijhon.config import ListenBrainzSettings, PlaylistSettings
from shijhon.playlists import listenbrainz as lb
from shijhon.playlists.history import History
from shijhon.playlists.listenbrainz import ListenBrainz, ListeningUnavailable
from shijhon.playlists.model import Connection, PlaylistError
from shijhon.playlists.similar import Similarity, SimilarUnavailable, in_turn
from tests.harness.fake_lastfm import KEY, FakeLastFm
from tests.harness.fake_listenbrainz import TOKEN, FakeListenBrainz, mbid
from tests.harness.logs import collected

pytestmark = pytest.mark.anyio

SEED = ("Velvet Harbor", "Night Tide")
TOGETHER = [("Copper Owls", "Low Lights"), ("Marrow Lane", "Paper Boats")]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def client(fake: FakeListenBrainz, clock: Clock | None = None, **settings: Any) -> ListenBrainz:
    found = ListenBrainz(
        ListenBrainzSettings(**settings), clock=clock or Clock(), transport=fake.transport()
    )
    found.pace = _NoPace()
    return found


class _NoPace:
    """Requests without the one-a-second pace (it is tested on its own)."""

    async def acquire(self) -> None:
        return None


def history(fake_lastfm: FakeLastFm | None, listening: ListenBrainz | None) -> History:
    found = History(None, PlaylistSettings())  # type: ignore[arg-type]  (no store needed)
    if fake_lastfm is not None:
        found.http = httpx.AsyncClient(transport=fake_lastfm.transport())
    found.listenbrainz = listening
    return found


# --- the client -----------------------------------------------------------------------------


async def test_a_song_s_listened_together_songs_by_its_canonical_recording() -> None:
    fake = FakeListenBrainz()
    recording = fake.song(*SEED, [*TOGETHER, SEED])
    listening = client(fake)
    found = await listening.similar_songs([SEED], 10)
    # The most similar first, each with a score of the first one's; never the seed itself.
    assert [(a, t) for a, t, _ in found] == TOGETHER
    assert found[0][2] == 1.0 and found[1][2] < 1.0
    lookup, similar = fake.requests
    # Names in one Labs lookup; then the recording it found, with a permitted algorithm.
    assert (lookup.method, lookup.host, lookup.path) == (
        "POST",
        "labs.api.listenbrainz.org",
        "/acr-lookup/json",
    )
    assert lookup.body == [{"artist_credit_name": SEED[0], "recording_name": SEED[1]}]
    assert similar.params == {"recording_mbids": recording, "algorithm": lb.SONGS_ALGORITHM}
    # Shijhon by name and version; no account, no token, no listening history.
    for asked in fake.requests:
        assert asked.headers["user-agent"].startswith("Shijhon/")
        assert "authorization" not in asked.headers


async def test_variants_are_looked_up_together_and_the_first_found_is_used() -> None:
    fake = FakeListenBrainz()
    fake.song("Velvet Harbor", "Night Tide", TOGETHER[:1])
    listening = client(fake)
    names = [("Velvet Harbor & Copper Owls", "Night Tide (feat. X)"), SEED]
    assert [(a, t) for a, t, _ in await listening.similar_songs(names, 10)] == TOGETHER[:1]
    assert len(fake.asked("/acr-lookup")) == 1
    assert len(fake.asked("/acr-lookup")[0].body) == 2


async def test_unknown_songs_have_none_and_answers_are_kept() -> None:
    fake = FakeListenBrainz()
    fake.song(*SEED, TOGETHER)
    listening = client(fake)
    assert await listening.similar_songs([("Nobody Invented", "Nothing")], 10) == ()
    for _ in range(3):
        await listening.similar_songs([SEED], 10)
        await listening.similar_songs([("Nobody Invented", "Nothing")], 10)
    # One lookup of each, one list, one search for the unknown song: "nothing known" is
    # kept as well, and a song found exactly with similar songs is never searched for.
    assert len(fake.asked("/acr-lookup")) == 2
    assert len(fake.asked("/similar-recordings")) == 1
    assert [r.params for r in fake.asked("/recording-search")] == [
        {"query": "Nobody Invented Nothing"}
    ]


async def test_with_a_token_a_song_labs_cannot_find_is_looked_up_loosely() -> None:
    fake = FakeListenBrainz()
    recording = fake.song(*SEED, TOGETHER[:2], loose=True)
    without = client(fake)
    assert await without.similar_songs([SEED], 10) == ()
    assert not fake.asked("/1/metadata")  # (it needs a token)
    listening = client(fake, token=TOKEN)
    assert [(a, t) for a, t, _ in await listening.similar_songs([SEED], 10)] == TOGETHER[:2]
    (metadata,) = fake.asked("/1/metadata/lookup")
    assert metadata.host == "api.listenbrainz.org"
    assert metadata.headers["authorization"] == f"Token {TOKEN}"
    assert metadata.params == {"artist_name": SEED[0], "recording_name": SEED[1]}
    assert fake.asked("/similar-recordings")[-1].params["recording_mbids"] == recording
    # The token goes to the metadata lookup only: never to Labs.
    labs = [r for r in fake.requests if r.host == "labs.api.listenbrainz.org"]
    assert labs and all("authorization" not in r.headers for r in labs)


async def test_a_refused_token_is_said_once_without_itself_and_does_not_rest() -> None:
    fake = FakeListenBrainz()
    fake.song(*SEED, TOGETHER[:2], loose=True)
    listening = client(fake, token="invented-wrong-token")
    with collected("shijhon.playlists.listenbrainz") as lines:
        assert await listening.similar_songs([SEED], 10) == ()
        assert await listening.similar_songs([("Other Invented", "Song")], 10) == ()
    assert len(fake.asked("/1/metadata")) == 1  # not asked again with the refused token
    assert sum("token was refused" in line for line in lines) == 1
    assert not any("invented-wrong-token" in line or SEED[0] in line for line in lines)
    assert listening.resting_until == 0.0
    # What was looked up while the token was refused is not kept as "nothing known".
    assert listening.known_songs([SEED]) is None


async def test_a_corrected_token_finds_what_the_refused_one_did_not() -> None:
    fake = FakeListenBrainz()
    fake.song(*SEED, TOGETHER[:2], loose=True)
    listening = client(fake, token="invented-wrong-token")
    assert await listening.similar_songs([SEED], 10) == ()
    # The administrator enters the right token on the dashboard: it applies at once.
    listening.settings = ListenBrainzSettings(token=TOKEN)
    assert [(a, t) for a, t, _ in await listening.similar_songs([SEED], 10)] == TOGETHER[:2]
    assert fake.asked("/1/metadata")[-1].headers["authorization"] == f"Token {TOKEN}"


async def test_another_token_forgets_what_the_first_one_found() -> None:
    fake = FakeListenBrainz()
    fake.song(*SEED, TOGETHER[:2], loose=True)
    listening = client(fake)  # no token: the loose lookup is not asked
    assert await listening.similar_songs([SEED], 10) == ()
    assert listening.known_songs([SEED]) == ()
    listening.settings = ListenBrainzSettings(token=TOKEN)
    assert listening.known_songs([SEED]) is None  # (found without the token: forgotten)
    assert len(await listening.similar_songs([SEED], 10)) == 2


async def test_an_artist_s_similar_artists_by_its_songs() -> None:
    fake = FakeListenBrainz()
    artist = fake.artist("Velvet Harbor", ["Night Tide", "Salt Hour"], ["Copper Owls", "Marrow"])
    fake.song("Velvet Harbor & Others", "Salt Hour")  # another credit: no vote
    listening = client(fake)
    found = await listening.similar_artists("Velvet Harbor", ["Night Tide", "Salt Hour"], 10)
    assert found == ["Copper Owls", "Marrow"]
    assert fake.asked("/similar-artists")[0].params == {
        "artist_mbids": artist,
        "algorithm": lb.ARTISTS_ALGORITHM,
    }
    # An artist none of whose songs it finds: none, and no similarity list asked.
    assert await listening.similar_artists("Nobody Invented", ["Nothing"], 10) == []
    assert await client(fake).similar_artists("Velvet Harbor", [], 10) == []  # (unknown who)
    # Kept by name: asked again, nothing is sent.
    sent = len(fake.requests)
    assert await listening.similar_artists("Velvet Harbor", [], 10) == ["Copper Owls", "Marrow"]
    assert len(fake.requests) == sent and len(fake.asked("/similar-artists")) == 1


async def test_a_failure_rests_listenbrainz_and_nothing_is_asked_meanwhile() -> None:
    fake = FakeListenBrainz()
    fake.song(*SEED, TOGETHER)
    clock = Clock()
    listening = client(fake, clock)
    fake.status = 503
    with collected("shijhon.playlists.listenbrainz") as lines:
        with pytest.raises(ListeningUnavailable):
            await listening.similar_songs([SEED], 10)
        fake.status = 200
        with pytest.raises(ListeningUnavailable):  # resting: at once, not asked
            await listening.similar_songs([SEED], 10)
    assert len(fake.requests) == 1
    assert any("not asked for 60s" in line for line in lines)
    assert not any(SEED[0] in line or SEED[1] in line for line in lines)
    clock.now += lb.REST_SECONDS
    assert len(await listening.similar_songs([SEED], 10)) == 2


async def test_a_rate_limit_rests_as_long_as_it_says_within_bounds() -> None:
    fake = FakeListenBrainz()
    fake.song(*SEED, TOGETHER)
    clock = Clock()
    listening = client(fake, clock)
    fake.status, fake.headers = 429, {"X-RateLimit-Reset-In": "7"}
    with pytest.raises(ListeningUnavailable):
        await listening.similar_songs([SEED], 10)
    assert listening.resting_until == clock.now + 7
    fake.headers = {"X-RateLimit-Reset-In": "999999"}
    clock.now += 7
    with pytest.raises(ListeningUnavailable):
        await listening.similar_songs([SEED], 10)
    assert listening.resting_until == clock.now + lb.LONGEST_REST
    # The last answer allowed is used, and then nothing is asked for its reset.
    clock.now += lb.LONGEST_REST
    fake.status, fake.headers = 200, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "3"}
    assert (await listening._lookup((SEED,)))[0] is not None
    assert listening.resting_until == clock.now + 3


async def test_slow_and_unreadable_answers_are_failures() -> None:
    fake = FakeListenBrainz()
    fake.song(*SEED, TOGETHER)
    clock = Clock()
    listening = client(fake, clock, timeout_seconds=0.2)
    fake.delay = 1.0
    started = time.monotonic()
    with pytest.raises(ListeningUnavailable):
        await listening.similar_songs([SEED], 10)
    assert time.monotonic() - started < 0.8
    fake.delay = 0.0
    clock.now += lb.REST_SECONDS
    listening.http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"{not json"))
    )
    with pytest.raises(ListeningUnavailable):
        await listening._lookup((SEED,))


async def test_turned_off_nothing_is_asked() -> None:
    fake = FakeListenBrainz()
    fake.song(*SEED, TOGETHER)
    listening = client(fake, enabled=False)
    with pytest.raises(ListeningUnavailable):
        await listening.similar_songs([SEED], 10)
    assert fake.requests == []
    # The setting is read at every request: the dashboard turns it on at once.
    listening.settings = ListenBrainzSettings(enabled=True)
    assert len(await listening.similar_songs([SEED], 10)) == 2


async def test_requests_are_paced_to_one_a_second() -> None:
    fake = FakeListenBrainz()
    listening = ListenBrainz(ListenBrainzSettings(), transport=fake.transport())
    started = time.monotonic()
    for at in range(3):
        await listening._lookup((("Invented", f"Song {at}"),))
    assert time.monotonic() - started >= 1.8


async def test_requests_waiting_for_their_turn_are_not_sent_after_a_rest_began() -> None:
    fake = FakeListenBrainz()
    fake.status, fake.delay = 503, 0.2  # (the others wait for their turn meanwhile)
    listening = ListenBrainz(ListenBrainzSettings(), transport=fake.transport())
    failed: list[str] = []

    async def ask(at: int) -> None:
        try:
            await listening._lookup((("Invented", f"Song {at}"),))
        except ListeningUnavailable:
            failed.append(f"Song {at}")

    async with anyio.create_task_group() as group:
        for at in range(3):
            group.start_soon(ask, at)
    # The first failure rests ListenBrainz: the two waiting for their turn are not sent.
    assert len(failed) == 3 and len(fake.requests) == 1


async def test_requests_waiting_for_their_turn_are_not_sent_once_turned_off() -> None:
    fake = FakeListenBrainz()
    listening = ListenBrainz(ListenBrainzSettings(), transport=fake.transport())
    await listening._lookup((("Invented", "First"),))  # (the turn of this second is taken)

    async def turn_off() -> None:
        await anyio.sleep(0.3)
        listening.settings = ListenBrainzSettings(enabled=False)

    async with anyio.create_task_group() as group:
        group.start_soon(turn_off)
        with pytest.raises(ListeningUnavailable):
            await listening._lookup((("Invented", "Second"),))
    assert len(fake.requests) == 1


async def test_the_timeout_covers_the_wait_for_a_turn_and_the_answer_together() -> None:
    fake = FakeListenBrainz()
    listening = ListenBrainz(ListenBrainzSettings(timeout_seconds=1.5), transport=fake.transport())
    await listening._lookup((("Invented", "First"),))  # the next turn is in a second
    fake.delay = 0.8  # (alone within the timeout; after the wait for a turn, not)
    started = time.monotonic()
    with pytest.raises(ListeningUnavailable):
        await listening._lookup((("Invented", "Second"),))
    assert time.monotonic() - started < 1.7
    assert listening.resting_until > 0  # (no answer in time: ListenBrainz's failure)


async def test_waiting_for_a_turn_too_long_is_no_failure_of_listenbrainz() -> None:
    fake = FakeListenBrainz()
    listening = ListenBrainz(ListenBrainzSettings(timeout_seconds=0.3), transport=fake.transport())
    await listening._lookup((("Invented", "First"),))
    with pytest.raises(ListeningUnavailable):
        await listening._lookup((("Invented", "Second"),))
    assert listening.resting_until == 0.0 and len(fake.requests) == 1


# --- the recording search, for songs the exact lookup does not find usefully ----------------


async def test_a_featured_credit_s_original_is_found_by_the_search() -> None:
    fake = FakeListenBrainz()
    fake.song("Velvet Harbor", "Night Tide")  # a compilation's copy: no similar songs
    fake.searched("The Covers Band", "Night Tide")  # another artist's: never taken
    fake.searched("Velvet Harbor", "Night Tide - Velvet Harbor Tribute")  # another title
    fake.searched("Velvet Harbor", "Night Tide", [])  # another copy, without its feature
    original = fake.searched("Velvet Harbor feat. Ana Brandt & Ilse", "Night Tide", TOGETHER)
    listening = client(fake)
    names = [("Velvet Harbor feat. Ana Brandt", "Night Tide (Radio Edit)"), SEED]
    found = await listening.similar_songs(names, 10)
    assert [(a, t) for a, t, _ in found] == TOGETHER
    # The search asks for the lead artist and the title without qualifiers.
    (search,) = fake.asked("/recording-search")
    assert search.params == {"query": "Velvet Harbor Night Tide"}
    assert fake.asked("/similar-recordings")[-1].params["recording_mbids"] == original
    # Kept for a day like the rest: nothing is asked again.
    sent = len(fake.requests)
    assert await listening.similar_songs(names, 10) == found
    assert len(fake.requests) == sent


async def test_the_search_takes_nothing_that_does_not_clearly_match() -> None:
    fake = FakeListenBrainz()
    fake.searched("The Covers Band", "Night Tide", TOGETHER)
    fake.searched("Velvet Harbor", "Night Tide Reprise", TOGETHER)
    listening = client(fake)
    assert await listening.similar_songs([SEED], 10) == ()
    assert not fake.asked("/similar-recordings")


async def test_the_search_takes_no_other_version_or_rendition_of_the_song() -> None:
    fake = FakeListenBrainz()
    fake.searched("Velvet Harbor", "Night Tide (Live)", [("Live", "Song")])
    fake.searched("Velvet Harbor", "Night Tide (Karaoke Version)", [("Karaoke", "Song")])
    fake.searched("Velvet Harbor", "Night Tide (Acoustic Demo)", [("Demo", "Song")])
    fake.searched("Velvet Harbor", "Night Tide (Club Remix)", [("Remix", "Song")])
    fake.searched("Velvet Harbor Tribute", "Night Tide", [("Tribute", "Song")])
    fake.searched("Velvet Harbor feat. The Tribute Choir", "Night Tide", [("Choir", "Song")])
    listening = client(fake)
    assert await listening.similar_songs([SEED], 10) == ()
    assert not fake.asked("/similar-recordings")
    # The same qualifier on the song itself: that version is the one wanted.
    found = await client(fake).similar_songs([("Velvet Harbor", "Night Tide (Live)")], 10)
    assert [(a, t) for a, t, _ in found] == [("Live", "Song")]


def test_version_words_are_told_by_the_names() -> None:
    same = lb.same_version
    assert same("A", "Night Tide", "A feat. B", "Night Tide (Remastered 2011)")
    assert same("A", "Get Lucky (Radio Edit)", "A feat. B", "Get Lucky")
    assert same("A", "Live Forever", "A", "Live Forever")  # (its title's own word)
    assert not same("A", "Night Tide", "A", "Night Tide - Live at the Hall")
    assert not same("A", "Night Tide", "A", "Night Tide (Instrumental)")
    assert not same("A", "Night Tide", "A Karaoke Band", "Night Tide")
    assert same("A", "Night Tide (Live)", "A", "Night Tide [live]")


async def test_the_search_is_not_asked_when_the_exact_lookup_found_similar_songs() -> None:
    fake = FakeListenBrainz()
    fake.song(*SEED, TOGETHER)
    fake.searched(*SEED, [("Other", "Song")])
    listening = client(fake)
    assert [(a, t) for a, t, _ in await listening.similar_songs([SEED], 10)] == TOGETHER
    assert not fake.asked("/recording-search")


# --- Last.fm and ListenBrainz together ------------------------------------------------------


def test_lists_are_taken_in_turn_each_once() -> None:
    first = [("A", "1"), ("B", "2"), ("C", "3")]
    second = [("b", "2"), ("D", "4")]
    found = in_turn([first, second, None], lambda s: (s[0].lower(), s[1]))
    assert found == [("A", "1"), ("b", "2"), ("D", "4"), ("C", "3")]


def lastfm_knows(*songs: tuple[str, str]) -> FakeLastFm:
    fake = FakeLastFm()
    fake.tracks[SEED] = list(songs)
    return fake


async def test_with_a_key_listenbrainz_is_never_waited_for_and_joins_once_known() -> None:
    lastfm = lastfm_knows(("Copper Owls", "Low Lights"), ("Ash Field", "Embers"))
    listening = FakeListenBrainz()
    listening.song(*SEED, TOGETHER)
    listening.delay = 3.0
    lb_client = client(listening, timeout_seconds=10)
    similarity = Similarity(history(lastfm, lb_client))
    started = time.monotonic()
    found = await similarity.tracks(SecretStr(KEY), [SEED], 50)
    assert time.monotonic() - started < 1.0 and listening.requests == []
    assert found.items == [("Copper Owls", "Low Lights"), ("Ash Field", "Embers")]
    assert found.waiting  # (ListenBrainz's part is still to come)
    listening.delay = 0.0
    await lb_client.similar_songs([SEED], 50)  # (as the radio asks it in the background)
    found = await similarity.tracks(SecretStr(KEY), [SEED], 50)
    # Last.fm's first (ListenBrainz's first is the same song), then each one's second.
    assert found.items == [("Copper Owls", "Low Lights"), ("Ash Field", "Embers"), TOGETHER[1]]
    assert not found.waiting
    # Turned off, nothing is to come.
    off = Similarity(history(lastfm, client(listening, enabled=False)))
    assert not (await off.tracks(SecretStr(KEY), [SEED], 50)).waiting


async def test_without_a_key_listenbrainz_is_waited_for_and_without_either_nothing() -> None:
    listening = FakeListenBrainz()
    listening.song(*SEED, TOGETHER[:2])
    similarity = Similarity(history(None, client(listening)))
    found = await similarity.tracks(None, [SEED], 50)
    assert found.items == TOGETHER[:2] and not found.waiting
    off = Similarity(history(None, client(listening, enabled=False)))
    assert off.listening is None
    with pytest.raises(SimilarUnavailable):
        await off.tracks(None, [SEED], 50)
    listening.status = 503
    with pytest.raises(SimilarUnavailable):
        await Similarity(history(None, client(listening))).tracks(None, [("A", "B")], 50)


async def test_a_failing_lastfm_is_no_answer_for_a_caller_with_a_key() -> None:
    lastfm = lastfm_knows(("Ash Field", "Embers"))
    lastfm.status = 500
    listening = FakeListenBrainz()
    listening.song(*SEED, TOGETHER)
    similarity = Similarity(history(lastfm, client(listening)))
    with pytest.raises(SimilarUnavailable):
        await similarity.tracks(SecretStr(KEY), [SEED], 50)


async def test_similar_artists_of_both_sources_in_turn() -> None:
    lastfm = FakeLastFm()
    lastfm.artists["Velvet Harbor"] = ["Copper Owls", "Ash Field"]
    listening = FakeListenBrainz()
    listening.artist("Velvet Harbor", ["Night Tide"], ["copper owls", "Marrow Lane"])
    asked: list[str] = []

    async def titles() -> list[str]:
        asked.append("titles")
        return ["Night Tide"]

    lb_client = client(listening)
    similarity = Similarity(history(lastfm, lb_client))
    found = await similarity.artists(SecretStr(KEY), "Velvet Harbor", 15, titles)
    assert found.items == ["Copper Owls", "Ash Field"] and found.waiting
    assert asked == []  # (not waited for: nothing asked)
    await lb_client.similar_artists("Velvet Harbor", ["Night Tide"], 15)
    found = await similarity.artists(SecretStr(KEY), "Velvet Harbor", 15, titles)
    assert found.items == ["Copper Owls", "Ash Field", "Marrow Lane"] and not found.waiting
    # Without a key: ListenBrainz waited for, with the artist's songs.
    keyless = Similarity(history(None, client(listening)))
    found = await keyless.artists(None, "Velvet Harbor", 15, titles)
    assert found.items == ["copper owls", "Marrow Lane"] and asked == ["titles"]


# --- discovery mixes' candidates ------------------------------------------------------------


def listens(*songs: tuple[str, str]) -> list[dict[str, Any]]:
    now = time.time()
    return [
        {"at": now - 3600 * at, "artist": a, "title": t, "album": "Invented"}
        for at, (a, t) in enumerate(songs)
    ]


CONNECTION = Connection(
    user_id="u",
    username="owner",
    token=SecretStr("t"),
    salt=SecretStr("s"),
    history_user="someone",
    history_key=SecretStr(KEY),
)


async def test_discovery_candidates_add_listenbrainz_s_and_score_both_sources_lists() -> None:
    lastfm = lastfm_knows(("Copper Owls", "Low Lights"), ("Ash Field", "Embers"))
    listening = FakeListenBrainz()
    listening.song(*SEED, [("Copper Owls", "Low Lights"), ("Marrow Lane", "Paper Boats")])
    found = await history(lastfm, client(listening)).candidates(CONNECTION, listens(SEED))
    by_song = {(c["artist"], c["title"]): c for c in found}
    # Listed by both: scored for both, first; ListenBrainz's own is a candidate too.
    assert [(c["artist"], c["title"]) for c in found][:3] == [
        ("Copper Owls", "Low Lights"),
        ("Ash Field", "Embers"),
        ("Marrow Lane", "Paper Boats"),
    ]
    assert (
        by_song[("Copper Owls", "Low Lights")]["score"] > by_song[("Ash Field", "Embers")]["score"]
    )
    only = by_song[("Marrow Lane", "Paper Boats")]
    assert only["lastfm"] is False and "Listened together" in only["reason"]
    assert "lastfm" not in by_song[("Copper Owls", "Low Lights")]
    assert by_song[SEED]["reason"] == "A familiar track from your imported history"
    # One lookup for all seeds, then the seed's list: names only.
    assert listening.asked("/acr-lookup")[0].body == [
        {"artist_credit_name": SEED[0], "recording_name": SEED[1]}
    ]


async def test_discovery_does_not_wait_for_or_fail_with_listenbrainz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shijhon.playlists import history as history_module

    monkeypatch.setattr(history_module, "LISTENING_GRACE", 0.3)
    lastfm = lastfm_knows(("Ash Field", "Embers"))
    listening = FakeListenBrainz()
    listening.song(*SEED, TOGETHER)
    listening.delay = 2.0
    started = time.monotonic()
    found = await history(lastfm, client(listening, timeout_seconds=10)).candidates(
        CONNECTION, listens(SEED)
    )
    assert time.monotonic() - started < 1.5
    assert [(c["artist"], c["title"]) for c in found] == [("Ash Field", "Embers"), SEED]
    listening.delay, listening.status = 0.0, 500
    found = await history(lastfm, client(listening)).candidates(CONNECTION, listens(SEED))
    assert [(c["artist"], c["title"]) for c in found] == [("Ash Field", "Embers"), SEED]
    # Last.fm's failure is still the run's.
    lastfm.status = 500
    with pytest.raises(PlaylistError):
        await history(lastfm, client(listening)).candidates(CONNECTION, listens(SEED))


def test_mbids_of_the_stand_in_are_well_formed(tmp_path: Path) -> None:
    assert lb.MBID.fullmatch(mbid("artist", "Velvet Harbor"))
