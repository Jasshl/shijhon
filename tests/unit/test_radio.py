"""Radio from catalog items: what is asked of Last.fm and how its answers are read, and
the lists a radio is put together from."""

from __future__ import annotations

import json
from typing import Any, cast

import anyio
import httpx
import pytest
from aiolimiter import AsyncLimiter
from pydantic import SecretStr

from shijhon.catalog.model import CatalogRef, CatalogTrack
from shijhon.playlists import similar
from shijhon.playlists.history import ENDPOINT, History
from shijhon.playlists.similar import Similarity, SimilarUnavailable
from shijhon.views.radio import _distinct, _in_turn, _lead, _somebody
from tests.harness.fake_lastfm import KEY, FakeLastFm

SECRET = SecretStr(KEY)


class Discovery:
    """What the similarity asks through: the discovery's HTTP client and its pace."""

    def __init__(self, lastfm: FakeLastFm, pace: AsyncLimiter | None = None) -> None:
        self.http = httpx.AsyncClient(transport=lastfm.transport())
        self.pace = pace or AsyncLimiter(100, 1)


def similarity(lastfm: FakeLastFm, pace: AsyncLimiter | None = None) -> Similarity:
    return Similarity(cast(History, Discovery(lastfm, pace)))


def test_similar_songs_are_asked_for_by_artist_and_title_and_nothing_else() -> None:
    async def main() -> None:
        lastfm = FakeLastFm()
        lastfm.tracks[("Ana Brandt", "Paper Boats")] = [
            ("Ilse Marek", "Low Tide"),
            ("Ilse Marek", "Low Tide"),  # listed twice: once
            ("Otto Vance", "Harbor Lights"),
        ]
        asking = similarity(lastfm)
        found = await asking.tracks(SECRET, "Ana Brandt", "Paper Boats", 50)
        assert found == [("Ilse Marek", "Low Tide"), ("Otto Vance", "Harbor Lights")]
        assert lastfm.requests == [
            {
                "api_key": KEY,
                "method": "track.getSimilar",
                "format": "json",
                "autocorrect": "1",
                "artist": "Ana Brandt",
                "track": "Paper Boats",
                "limit": "50",
            }
        ]
        # A song Last.fm does not know has no similar songs: that is an answer.
        assert await asking.tracks(SECRET, "Ana Brandt", "Unheard", 50) == []
        assert await asking.tracks(SECRET, "Ana Brandt", "Paper Boats", 1) == found[:1]
        assert asking.requests == 3

    anyio.run(main)


def test_similar_artists_are_asked_for_by_name() -> None:
    async def main() -> None:
        lastfm = FakeLastFm()
        lastfm.artists["Ana Brandt"] = ["Ilse Marek", "Otto Vance", "Ilse Marek"]
        asking = similarity(lastfm)
        assert await asking.artists(SECRET, "Ana Brandt", 15) == ["Ilse Marek", "Otto Vance"]
        assert lastfm.requests[0] == {
            "api_key": KEY,
            "method": "artist.getSimilar",
            "format": "json",
            "autocorrect": "1",
            "artist": "Ana Brandt",
            "limit": "15",
        }
        assert await asking.artists(SECRET, "Nobody Heard", 15) == []

    anyio.run(main)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # One item comes as an object, not a list.
        ({"similartracks": {"track": {"name": "Low Tide", "artist": {"name": "Ilse Marek"}}}},
         [("Ilse Marek", "Low Tide")]),
        # An artist as plain text; entries without an artist or a title are left out.
        ({"similartracks": {"track": [{"name": "Low Tide", "artist": "Ilse Marek"},
                                      {"name": "", "artist": {"name": "Ilse Marek"}},
                                      {"name": "Untitled"}, "junk", None]}},
         [("Ilse Marek", "Low Tide")]),
        # Names with line breaks or controls are never search terms; spaces are joined.
        ({"similartracks": {"track": [{"name": "Low‮Tide", "artist": {"name": "Ilse"}},
                                      {"name": "Low   Tide ", "artist": {"name": " Ilse  M "}}]}},
         [("Ilse M", "Low Tide")]),
        ({"similartracks": {"track": []}}, []),
        ({"similartracks": {}}, []),
    ],
)  # fmt: skip
def test_the_shapes_lastfm_answers_in(answer: dict[str, Any], expected: list[Any]) -> None:
    async def main() -> None:
        lastfm = FakeLastFm()
        lastfm.body = json.dumps(answer).encode()
        assert await similarity(lastfm).tracks(SECRET, "Ana Brandt", "Paper Boats", 50) == expected

    anyio.run(main)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"similartracks": "none"}',
        b'{"error": 29, "message": "Rate limit exceeded"}',
        b'{"similartracks": {"track": []}, "padding": "' + b"x" * similar.BODY_LIMIT + b'"}',
    ],
)
def test_an_answer_that_is_none_is_unavailable_never_an_empty_list(body: bytes) -> None:
    async def main() -> None:
        lastfm = FakeLastFm()
        lastfm.body = body
        with pytest.raises(SimilarUnavailable):
            await similarity(lastfm).tracks(SECRET, "Ana Brandt", "Paper Boats", 50)
        with pytest.raises(SimilarUnavailable):
            await similarity(lastfm).artists(SECRET, "Ana Brandt", 15)

    anyio.run(main)


def test_a_failing_or_slow_lastfm_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        lastfm = FakeLastFm()
        lastfm.tracks[("Ana Brandt", "Paper Boats")] = [("Ilse Marek", "Low Tide")]
        lastfm.status = 503
        with pytest.raises(SimilarUnavailable):
            await similarity(lastfm).tracks(SECRET, "Ana Brandt", "Paper Boats", 50)
        # An error status is no answer, whatever its body says.
        lastfm.body = b'{"similartracks": {"track": []}, "similarartists": {"artist": []}}'
        with pytest.raises(SimilarUnavailable):
            await similarity(lastfm).tracks(SECRET, "Ana Brandt", "Paper Boats", 50)
        with pytest.raises(SimilarUnavailable):
            await similarity(lastfm).artists(SECRET, "Ana Brandt", 15)
        lastfm.body = None
        lastfm.status, lastfm.delay = 200, 5.0
        monkeypatch.setattr(similar, "SIMILAR_SECONDS", 0.2)
        started = anyio.current_time()
        with pytest.raises(SimilarUnavailable):
            await similarity(lastfm).tracks(SECRET, "Ana Brandt", "Paper Boats", 50)
        assert anyio.current_time() - started < 2.0

        async def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        asking = similarity(lastfm)
        asking.history.http = httpx.AsyncClient(transport=httpx.MockTransport(unreachable))
        with pytest.raises(SimilarUnavailable):
            await asking.artists(SECRET, "Ana Brandt", 15)

    anyio.run(main)


def test_requests_go_to_lastfm_at_the_discovery_s_pace() -> None:
    """The pace is the discovery's own: both together stay within it."""

    async def main() -> None:
        lastfm = FakeLastFm()
        lastfm.artists["Ana Brandt"] = ["Ilse Marek"]
        seen: list[str] = []

        async def note(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url.copy_with(query=None)))
            return await lastfm(request)

        pace = AsyncLimiter(2, 0.6)
        asking = similarity(lastfm, pace)
        asking.history.http = httpx.AsyncClient(transport=httpx.MockTransport(note))
        started = anyio.current_time()
        async with anyio.create_task_group() as group:
            for _ in range(4):
                group.start_soon(asking.artists, SECRET, "Ana Brandt", 15)
        assert anyio.current_time() - started >= 0.5  # two at once, the others after
        assert seen == [ENDPOINT] * 4

    anyio.run(main)


def song(ident: str, isrc: str | None = None) -> CatalogTrack:
    return CatalogTrack(
        CatalogRef("demo", ident), f"Song {ident}", "Ana Brandt", 180_000, isrc=isrc
    )


def test_each_song_and_each_recording_once_and_never_the_seed() -> None:
    seed = song("1", "ZZSHJ9900001")
    listed = [
        song("2", "ZZSHJ9900002"),
        song("1"),  # the seed itself
        song("3", "zzshj9900001"),  # the seed's recording on another release
        song("2", "ZZSHJ9900002"),
        song("4", "ZZSHJ9900002"),  # that recording again
        song("5"),
        song("5"),
        song("6"),
    ]
    assert [t.ref.id for t in _distinct(listed, seed)] == ["2", "5", "6"]
    assert [t.ref.id for t in _distinct(listed)] == ["2", "1", "3", "5", "6"]


def test_lists_in_turn() -> None:
    assert _in_turn([[1, 2, 3], [], [10], [20, 21]]) == [1, 10, 20, 2, 21, 3]
    assert _in_turn([]) == [] and _in_turn([[], []]) == []


def test_the_name_asked_of_lastfm() -> None:
    assert _lead("Ana Brandt feat. Otto Vance") == "Ana Brandt"
    assert _lead("Quill & Marrow") == "Quill & Marrow"  # a duo's name stays whole
    assert _somebody("Ana Brandt") and not _somebody("Various Artists")
    assert not _somebody("") and not _somebody(" - ")
