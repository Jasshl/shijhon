"""The AudioMuse-AI connection's parts: how its analysis is read, the percentiles, the steering
words, the settings, and the errors - which carry neither what it answered nor the token."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from shijhon.audiomuse import client as api
from shijhon.audiomuse.client import AudioMuseClient, Concepts, Track
from shijhon.audiomuse.facts import Analysis, Distribution, analysis, percentile, scores
from shijhon.audiomuse.service import AudioMuse
from shijhon.config import AudioMuseSettings
from shijhon.delivery.netpolicy import Reach
from shijhon.playlists.sound import FEATURES, SoundUnavailable

TOKEN = "tok-9f3c1d7a5b2e4c60"  # an invented token


def track(**more: Any) -> Track:
    base: dict[str, Any] = {
        "id": "s1",
        "fp": "abc",
        "tempo": 128.4,
        "key": "A",
        "scale": "minor",
        "energy": 0.62,
        "mood_vector": "rock:0.612,pop:0.301,indie:0.2,alternative:0.15,punk:0.1,folk:0.05",
        "other_features": (
            "danceable:0.62,aggressive:0.51,happy:0.55,party:0.58,relaxed:0.4,sad:0.45"
        ),
    }
    return Track(**{**base, **more})


# --- reading the analysis ------------------------------------------------------------------


def test_the_analysis_is_read_as_audiomuse_stores_it() -> None:
    facts = analysis(track())
    assert facts is not None
    assert facts.song_id == "s1" and facts.fingerprint == "abc"
    assert facts.tempo == 128.4 and facts.key == "A minor" and facts.energy == 0.62
    assert facts.labels == ["rock", "pop", "indie", "alternative", "punk"]  # five, strongest first
    assert facts.features == {
        "danceable": 0.62,
        "aggressive": 0.51,
        "happy": 0.55,
        "party": 0.58,
        "relaxed": 0.4,
        "sad": 0.45,
    }


def test_features_not_scored_yet_are_no_facts() -> None:
    zeros = ",".join(f"{name}:0.00" for name in FEATURES)
    facts = analysis(track(other_features=zeros))
    assert facts is not None and facts.features == {}  # (the rest is still kept)
    assert analysis(track(other_features=None)).features == {}  # type: ignore[union-attr]


def test_silent_audio_has_no_facts() -> None:
    # A placeholder that was analyzed: its energy is exactly 0.
    assert analysis(track(energy=0.0)) is None
    assert analysis(track(energy=-0.3)) is None
    assert analysis(track(energy=None)) is not None  # (an older analysis without energy)


def test_odd_values_are_left_out_not_trusted() -> None:
    facts = analysis(
        track(
            tempo="fast",
            key="H",
            scale="major",
            energy=float("nan"),
            mood_vector="rock,pop:x,:0.4,punk:0.2",
            other_features="danceable:0.5,bogus:0.9,sad:oops",
        )
    )
    assert facts is not None
    assert (facts.tempo, facts.key, facts.energy) == (None, "", None)
    assert facts.labels == ["punk"] and facts.features == {"danceable": 0.5}
    assert (
        analysis(track(tempo=None, key=None, energy=None, mood_vector="", other_features=""))
        is None
    )


def test_scores_keep_their_order_and_labels_with_spaces() -> None:
    assert scores("indie rock:0.5, classic rock:0.25,Hip-Hop:1") == {
        "indie rock": 0.5,
        "classic rock": 0.25,
        "Hip-Hop": 1.0,
    }
    assert scores(None) == {} and scores(12) == {}


# --- percentiles ---------------------------------------------------------------------------


def test_a_percentile_is_the_share_of_songs_below_with_ties_counting_half() -> None:
    values = [0.1, 0.2, 0.3, 0.4]
    assert [percentile(values, v) for v in values] == [12, 38, 62, 88]
    assert percentile([0.5], 0.5) == 50  # the only one
    assert percentile([0.5, 0.5, 0.5, 0.5], 0.5) == 50  # all tied
    assert percentile([], 0.5) == 50
    assert all(0 <= percentile(sorted(range(1000)), v) <= 100 for v in range(0, 1000, 37))


def row(song: str, energy: float | None, **features: float) -> Analysis:
    return Analysis(song, "fp", 120.0, "C major", energy, dict(features), ["rock"])


def test_the_distribution_places_songs_among_the_songs_that_have_the_fact() -> None:
    rows = [row("a", 0.2, happy=0.3), row("b", 0.5, happy=0.6), row("c", 0.9), row("d", None)]
    distribution = Distribution([r.energy for r in rows], [r.features for r in rows])
    low, mid, high, unknown = (distribution.sound(r) for r in rows)
    assert low.energy is not None and mid.energy is not None and high.energy is not None
    assert low.energy < mid.energy < high.energy and unknown.energy is None
    assert low.features == {"happy": 25} and mid.features == {"happy": 75}
    assert high.features == {}  # no score of its own: none invented
    assert low.tempo == 120.0 and low.key == "C major" and low.labels == ["rock"]


# --- steering ------------------------------------------------------------------------------


class Vocabulary:
    """Stands in for the client where only the steering vocabulary is asked."""

    def __init__(self, concepts: Concepts | Exception) -> None:
        self.concepts_answer = concepts
        self.asked = 0

    async def concepts(self) -> Concepts:
        self.asked += 1
        if isinstance(self.concepts_answer, Exception):
            raise self.concepts_answer
        return self.concepts_answer


def steering_of(vocabulary: Vocabulary) -> AudioMuse:
    sound = AudioMuse.__new__(AudioMuse)
    sound.client = vocabulary  # type: ignore[assignment]
    sound._concepts, sound._concepts_at, sound.clock = None, 0.0, lambda: 1000.0
    return sound


@pytest.mark.anyio
async def test_words_become_the_vocabularys_terms_and_unknown_ones_are_dropped() -> None:
    vocabulary = Vocabulary(Concepts(True, ("piano", "House", "slow tempo", "sad"), 3))
    sound = steering_of(vocabulary)
    found = await sound._steering(["Piano", "house", "dubstep"], ["  SLOW   tempo", "sad", "piano"])
    # "piano" is asked for both ways: neither; the limit is the vocabulary's (3 terms)
    assert found == [
        {"term": "House", "direction": "more"},
        {"term": "slow tempo", "direction": "less"},
        {"term": "sad", "direction": "less"},
    ]
    assert await sound._steering(["sad"], []) == [{"term": "sad", "direction": "more"}]
    assert vocabulary.asked == 1  # fetched once
    assert await sound._steering([], []) == []


@pytest.mark.anyio
async def test_no_steering_without_a_vocabulary_and_the_search_goes_on() -> None:
    unavailable = steering_of(Vocabulary(Concepts(False, (), 10)))
    assert await unavailable._steering(["piano"], []) == []
    broken = steering_of(Vocabulary(SoundUnavailable("down")))
    assert await broken._steering(["piano"], []) == []


@pytest.mark.anyio
async def test_a_vocabulary_that_was_unavailable_is_asked_again_later() -> None:
    vocabulary = Vocabulary(Concepts(False, (), 10))
    sound = steering_of(vocabulary)
    clock = [1000.0]
    sound.clock = lambda: clock[0]
    await sound._steering(["piano"], [])
    await sound._steering(["piano"], [])
    assert vocabulary.asked == 1
    clock[0] += 601
    vocabulary.concepts_answer = Concepts(True, ("piano",), 10)
    assert await sound._steering(["piano"], []) == [{"term": "piano", "direction": "more"}]
    assert vocabulary.asked == 2


# --- settings ------------------------------------------------------------------------------


def test_settings_default_to_off_and_check_the_address() -> None:
    off = AudioMuseSettings()
    assert off.url == "" and off.reach == "public" and off.sync_hours == 6.0
    assert AudioMuseSettings(url=" http://am.lan:8000/ ").url == "http://am.lan:8000"
    assert AudioMuseSettings(url="https://x.example/muse/").url == "https://x.example/muse"
    for bad in (
        "ftp://x",
        "am.lan:8000",
        "http://user:pw@x/",
        "http://x/?token=1",
        "http://x/#a",
        "http://x/a/../b",
    ):
        with pytest.raises(ValidationError):
            AudioMuseSettings(url=bad)
    for bad_hours in (0, -1):
        with pytest.raises(ValidationError):
            AudioMuseSettings(sync_hours=bad_hours)
    with pytest.raises(ValidationError):
        AudioMuseSettings(timeout_seconds=0)


def test_the_token_is_a_secret() -> None:
    settings = AudioMuseSettings(url="http://am.lan", token=SecretStr(TOKEN))
    assert TOKEN not in repr(settings) and TOKEN not in str(settings.model_dump())
    assert settings.api_token() == TOKEN
    assert AudioMuseSettings().api_token() == ""


# --- errors --------------------------------------------------------------------------------

SECRET_BODY = "the database at db.internal:5432 says: password=hunter2"


def clean(exc: BaseException) -> bool:
    """Nothing of what went wrong underneath is attached to the error."""
    return exc.__cause__ is None and (exc.__context__ is None or exc.__suppress_context__)


def client_for(handler: Any, **more: Any) -> AudioMuseClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AudioMuseClient("http://muse.internal:8000/base", TOKEN, http=http, **more)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, api.TOKEN_REFUSED),
        (403, api.TOKEN_REFUSED),
        (400, api.REFUSED),
        (302, api.REFUSED),
        (500, api.FAILED),
        (502, api.NOT_READY),
        (503, api.NOT_READY),
    ],
)
async def test_a_status_is_a_fixed_message_without_the_body(status: int, message: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=SECRET_BODY, headers={"location": "http://x/login"})

    client = client_for(handler)
    with pytest.raises(SoundUnavailable) as caught:
        await client.version()
    assert str(caught.value) == message
    for secret in (SECRET_BODY, TOKEN, "muse.internal", "hunter2", "/base"):
        assert secret not in str(caught.value) and secret not in repr(caught.value)
    assert clean(caught.value)
    await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (httpx.ConnectError("refused: muse.internal"), api.UNREACHABLE),
        (httpx.ReadTimeout("slow: muse.internal"), api.SLOW),
        (httpx.RemoteProtocolError("garbled"), api.UNREACHABLE),
    ],
)
async def test_a_failed_connection_is_a_fixed_message(failure: Exception, message: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    client = client_for(handler)
    with pytest.raises(SoundUnavailable) as caught:
        await client.manifest(1)
    assert str(caught.value) == message and "muse.internal" not in repr(caught.value)
    assert clean(caught.value)
    await client.aclose()


@pytest.mark.anyio
async def test_a_404_is_nothing_found_where_that_is_an_answer_and_an_old_version_elsewhere() -> (
    None
):
    client = client_for(lambda request: httpx.Response(404, json={"error": "not found"}))
    assert await client.similar_tracks("a", 10) == []
    assert await client.path("a", "b", 10) == []
    assert await client.alchemy(["a"], [], 10) == []
    assert await client.search("calm", 10) == []
    for call in (client.manifest(1), client.tracks(["a"]), client.concepts(), client.version()):
        with pytest.raises(SoundUnavailable) as caught:
            await call
        assert str(caught.value) == api.TOO_OLD
    await client.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("body", ["not json", "[]", '{"tracks": "x"}', '{"tracks": 3}'])
async def test_an_answer_of_the_wrong_shape_is_unreadable(body: str) -> None:
    client = client_for(lambda request: httpx.Response(200, text=body))
    with pytest.raises(SoundUnavailable) as caught:
        await client.manifest(1)
    assert str(caught.value) == api.UNREADABLE
    await client.aclose()


@pytest.mark.anyio
async def test_the_token_goes_in_the_header_to_the_address_only_and_never_into_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/api/sync"):
            return httpx.Response(200, json={"tracks": [{"id": "a", "fp": "1"}], "has_more": False})
        return httpx.Response(401, text=SECRET_BODY)

    caplog.set_level(logging.DEBUG)
    client = client_for(handler, server="Shijhon")
    assert await client.manifest(1) == ([("a", "1")], False)
    with pytest.raises(SoundUnavailable):
        await client.version()
    assert TOKEN not in repr(client) and TOKEN not in str(client.__dict__.get("_url", ""))
    first = seen[0]
    assert first.headers["authorization"] == f"Bearer {TOKEN}"
    assert first.url.host == "muse.internal" and first.url.path == "/base/api/sync"
    assert dict(first.url.params) == {
        "fields": "index",
        "limit": "1000",
        "page": "1",
        "server": "Shijhon",
    }
    assert TOKEN not in caplog.text and SECRET_BODY not in caplog.text
    await client.aclose()


@pytest.mark.anyio
async def test_a_json_body_carries_the_server_and_no_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    client = client_for(handler, server="srv-1")
    assert await client.alchemy(["a", "b"], ["c"], 10) == []
    body = seen[0].content.decode()
    assert '"server":"srv-1"' in body.replace(" ", "") and TOKEN not in body
    assert "server" not in dict(seen[0].url.params)
    await client.aclose()


@pytest.mark.anyio
async def test_a_redirect_is_not_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://elsewhere.invalid/login"})

    client = client_for(handler)
    with pytest.raises(SoundUnavailable):
        await client.version()
    assert len(seen) == 1
    await client.aclose()


@pytest.mark.anyio
async def test_the_network_policy_refuses_a_private_address_unless_allowed() -> None:
    async def resolver(host: str, port: int) -> list[str]:
        return ["10.1.2.3"]

    client = AudioMuseClient("http://muse.example.invalid:8000", TOKEN, resolver=resolver)
    with pytest.raises(SoundUnavailable) as caught:
        await client.version()
    assert str(caught.value) == api.DENIED
    await client.aclose()
    assert Reach("private") is Reach.PRIVATE


# --- the app's connection ------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_app_connects_only_when_an_address_is_set(tmp_path: Any) -> None:
    from shijhon.app import ShijhonApp
    from shijhon.config import load_settings
    from shijhon.store import Store

    store = await Store.open(tmp_path / "state.sqlite3")

    def app_with(**audiomuse: Any) -> ShijhonApp:
        settings = load_settings(None, state_dir=tmp_path, audiomuse=audiomuse)
        return ShijhonApp(settings)

    try:
        off = app_with()
        assert off._sound(off.settings, store) is None
        on = app_with(url="http://muse.lan:8000", token=TOKEN, server="Home", reach="private")
        sound = on._sound(on.settings, store)
        assert sound is not None
        assert TOKEN not in repr(sound) and TOKEN not in repr(sound.client)
        await sound.aclose()
        for part in (on, off):
            await part.upstream.aclose()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_a_token_held_back_is_never_sent_and_says_so(tmp_path: Any) -> None:
    from shijhon.app import ShijhonApp
    from shijhon.config import load_settings
    from shijhon.store import Store

    store = await Store.open(tmp_path / "state.sqlite3")
    app = ShijhonApp(load_settings(None, state_dir=tmp_path, audiomuse={"url": "http://m.lan"}))
    try:
        sound = app._sound(app.settings, store, token_held=True)
        assert sound is not None
        for call in (sound.client.version(), sound.client.manifest(1), sound.client.concepts()):
            with pytest.raises(SoundUnavailable) as caught:
                await call
            assert str(caught.value) == api.TOKEN_MOVED
        status = await sound.status()
        assert (status.reachable, status.error) == (False, api.TOKEN_MOVED)
        await sound.aclose()
        await app.upstream.aclose()
    finally:
        await store.close()
