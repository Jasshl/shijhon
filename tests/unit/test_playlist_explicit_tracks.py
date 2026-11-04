"""Explicit tracks publish what resolves and report every track: invented music only."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from pydantic import ValidationError

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.playlists.model import EDITED, Suggestion, Track
from shijhon.playlists.prompt import Unresolved, generate, summary
from shijhon.playlists.requests import PlaylistRequest, RequestError, TrackAddition
from shijhon.playlists.resolve import Explanation, Resolver, choose, originals
from shijhon.playlists.service import Playlists
from shijhon.views.ids import song_id
from tests.unit.test_playlist_requests import connected, service_at

ARTIST = "Velane"


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    from shijhon.playlists import prompt

    monkeypatch.setattr(prompt, "RETRY_PAUSE", 0.0)


def wanted(title: str, album: str = "", version: str = "") -> dict[str, Any]:
    suggestion = Suggestion(artist=ARTIST, title=title, album=album, version=version)
    return {
        **suggestion.wanted(),
        "strict_album": True,
        "prefer_original": True,
        "lenient_credit": True,
    }


def owned(ident: str, title: str, album: str, year: int = 0, **kw: Any) -> dict[str, Any]:
    return {"id": ident, "artist": ARTIST, "title": title, "album": album, "year": year, **kw}


def library(songs: list[dict[str, Any]] | None = None) -> AsyncMock:
    native = AsyncMock()
    native.native.library_id = 1
    native.call.return_value = {"searchResult3": {"song": songs or []}}
    return native


@dataclass
class Catalog:
    """Invented releases: ``(album title, year, songs)``, each song ``(own, title, isrc)``."""

    releases: list[tuple[str, str, list[tuple[str, str, str]]]] = field(default_factory=list)
    artist: str = ARTIST
    failure: str = ""
    key: str = "demo"

    def tracks(self) -> list[CatalogTrack]:
        return [
            CatalogTrack(
                CatalogRef(self.key, own),
                title,
                self.artist,
                200_000,
                isrc=isrc or None,
                album=CatalogRef(self.key, f"al{number}"),
                album_title=album,
                release_date=year,
            )
            for number, (album, year, songs) in enumerate(self.releases)
            for own, title, isrc in songs
        ]

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        if self.failure:
            raise CatalogError(self.failure, "invented failure")
        return SearchResults(songs=tuple(self.tracks()))

    async def song(self, ident: str) -> CatalogTrack:
        return next(t for t in self.tracks() if t.ref.id == ident)

    async def album(self, ident: str) -> CatalogRelease:
        listed = tuple(t for t in self.tracks() if t.album and t.album.id == ident)
        if not listed:
            raise CatalogError("not_found", "no such album")
        return CatalogRelease(
            CatalogRef(self.key, ident),
            listed[0].album_title or "",
            self.artist,
            ReleaseKind.ALBUM,
            listed[0].release_date,
            tracks=listed,
        )


def test_several_versions_resolve_to_the_original_only_when_asked() -> None:
    songs = [
        Track(id="a", artist=ARTIST, title="Night Bloom", album="Rooms (Deluxe)", year=2021),
        Track(id="b", artist=ARTIST, title="Night Bloom (2019 Remaster)", album="Rooms", year=2019),
        Track(id="c", artist=ARTIST, title="Night Bloom", album="Rooms", year=1998, clean=True),
        Track(id="d", artist=ARTIST, title="Night Bloom", album="Hits of Rooms", year=2004),
        Track(id="e", artist=ARTIST, title="Night Bloom", album="Rooms", year=1998),
    ]
    assert [s.id for s in originals(songs)] == ["e", "d", "a", "b", "c"]
    strict = {k: v for k, v in wanted("Night Bloom").items() if k != "prefer_original"}
    assert choose(strict, songs) is None  # other callers stay conservative
    found = choose(wanted("Night Bloom"), songs)
    assert found is not None and found.id == "e"
    # A requested album or version still decides first.
    found = choose(wanted("Night Bloom", album="Hits of Rooms"), songs)
    assert found is not None and found.id == "d"


def test_the_library_s_original_comes_before_the_catalog() -> None:
    async def run() -> None:
        catalog = Catalog([("Rooms", "1998", [("c1", "Night Bloom", "XX1")])])
        native = library(
            [
                owned("n2", "Night Bloom", "Rooms (Expanded Edition)", 2015, isrc="XX2"),
                owned("n1", "Night Bloom", "Rooms", 1998, isrc="XX1"),
            ]
        )
        found = await Resolver(native, catalog).resolve(connected(), wanted("Night Bloom"))  # type: ignore[arg-type]
        assert found is not None and found.id == "n1"

    anyio.run(run)


def test_the_catalog_s_original_release_is_chosen_among_versions() -> None:
    async def run() -> None:
        catalog = Catalog(
            [
                ("Rooms (Remastered)", "2016", [("c2", "Night Bloom (Remastered)", "XX2")]),
                ("Halloween Party Hits", "2012", [("c3", "Night Bloom", "XX3")]),
                ("Rooms", "1998", [("c1", "Night Bloom", "XX1")]),
            ]
        )
        found = await Resolver(library(), catalog).resolve(connected(), wanted("Night Bloom"))  # type: ignore[arg-type]
        assert found is not None and found.id == song_id(CatalogRef("demo", "c1"))
        assert found.album == "Rooms" and found.year == 1998

    anyio.run(run)


def one(title: str, album: str = "Rooms", artist: str = ARTIST) -> Catalog:
    return Catalog([(album, "1998", [("c1", title, "")])], artist=artist)


BLOOM = wanted("Night Bloom")


@pytest.mark.parametrize(
    ("catalog", "track", "reason", "candidates"),
    [
        (one("Night Bloom"), wanted("Night Bloom", album="Halls"), "album_mismatch",
         ["Night Bloom / Rooms"]),
        (one("Night Bloom (Live)", "Live at Halls"), BLOOM, "version_mismatch",
         ["Night Bloom (Live) / Live at Halls"]),
        (one("Night Bloom - Main Title", "Score"), BLOOM, "version_mismatch",
         ["Night Bloom - Main Title / Score"]),
        (one("Night Bloom", artist=f"{ARTIST} & The Lanterns"), BLOOM, "artist_mismatch",
         ["Night Bloom / Rooms"]),
        (one("Night Bloom (Live)", artist=f"{ARTIST} feat. Ada"), BLOOM, "version_mismatch",
         ["Night Bloom (Live) / Rooms"]),
        (one("Bloom", artist=f"{ARTIST} Trio"), wanted("The Bloom"), "title_mismatch",
         ["Bloom / Rooms"]),
        (one("Night Bloom Waltz"), BLOOM, "title_mismatch", ["Night Bloom Waltz / Rooms"]),
        (one("Night Bloom", artist="Someone Else"), BLOOM, "not_found", []),
        (one("Dawn", artist=ARTIST), BLOOM, "not_found", []),
        (Catalog(failure="unavailable"), BLOOM, "catalog_unavailable", []),
        (Catalog(failure="rate_limited"), BLOOM, "catalog_unavailable", []),
        (Catalog(failure="invalid"), BLOOM, "catalog_unavailable", []),
        (Catalog(failure="not_found"), BLOOM, "not_found", []),
    ],
)  # fmt: skip
def test_a_left_out_track_says_why_with_the_nearest_songs(
    catalog: Catalog, track: dict[str, Any], reason: str, candidates: list[str]
) -> None:
    async def run() -> None:
        resolver = Resolver(library(), catalog)  # type: ignore[arg-type]
        assert await resolver.resolve(connected(), track) is None
        explanation = await resolver.explain(connected(), track)
        assert explanation.reason == reason
        assert [f"{c['title']} / {c['album']}" for c in explanation.candidates()] == candidates

    anyio.run(run)


def test_candidates_are_few_distinct_music_names() -> None:
    songs = [Track(id=f"x{i}", artist=ARTIST, title="T", album=f"A{i % 7}") for i in range(30)]
    shown = Explanation("version_mismatch", songs).candidates()
    assert len(shown) == 5 and shown[0] == {"artist": ARTIST, "title": "T", "album": "A0"}


# --- runs ------------------------------------------------------------------------------

SONGS = {
    "Night Bloom": Track(id="n1", artist=ARTIST, title="Night Bloom", album="Rooms"),
    "Lantern Hall": Track(id="n2", artist="Orrin Vale", title="Lantern Hall", album="Halls"),
    "Grey Tide": Track(id="n3", artist="Mira Sole", title="Grey Tide", album="Shore"),
    "Paper Moths": Track(id="n4", artist="Mira Sole", title="Paper Moths", album="Shore"),
}
CATALOG_SONG = Track(
    id=song_id(CatalogRef("demo", "c9")), artist="Orrin Vale", title="Far Signal", album="Signals"
)


def install(service: Playlists, missing: str = "not_found") -> dict[str, Any]:
    """Library songs ``SONGS``; any other title is left out for ``missing``; a native
    playlist the run publishes."""
    native: dict[str, Any] = {}
    by_id = {s.id: s for s in SONGS.values()}

    async def resolve(connection: Any, want: dict[str, Any], **kw: Any) -> Track | None:
        assert want["prefer_original"] and want["strict_album"] and want["lenient_credit"]
        found = SONGS.get(want["title"])
        if found is None and missing == "catalog_unavailable":
            kw["failures"].append("unavailable")
        return found

    async def explain(connection: Any, want: dict[str, Any], **kw: Any) -> Explanation:
        assert kw["unanswered"] is (missing == "catalog_unavailable")
        near = Track(id="x", artist=want["artist"], title=f"{want['title']} (Live)", album="Hall")
        return Explanation(missing, [near])  # type: ignore[arg-type]

    async def call(connection: Any, method: str, params: Any = ()) -> Any:
        values = dict(params)
        if method == "getSong":
            return {"song": by_id[values["id"]].model_dump()}
        if method == "createPlaylist":
            if "playlistId" not in values:
                native.update(id="explicit-playlist", name=values["name"], owner="owner")
            native["entry"] = [{"id": v} for k, v in params if k == "songId"]
            native["changed"] = len(native.get("changed", "")) * "x" + "x"
            return {"playlist": dict(native)}
        assert method == "updatePlaylist"
        native["name"] = values["name"]
        return {}

    service.resolver.resolve = resolve  # type: ignore[method-assign]
    service.resolver.explain = explain  # type: ignore[method-assign]
    service.client.call = call  # type: ignore[method-assign]
    service.client.playlist = AsyncMock(side_effect=lambda *args: dict(native))  # type: ignore[method-assign]
    service.client.native.song = AsyncMock(return_value={"libraryId": 1})
    return native


def request(*titles: str, **kw: Any) -> PlaylistRequest:
    artists = {t: SONGS[t].artist if t in SONGS else ARTIST for t in titles}
    return PlaylistRequest(
        name="Lantern evening",
        count=len(titles),
        tracks=[{"artist": artists[t], "title": t} for t in titles],
        **kw,
    )


async def published(service: Playlists, spec: PlaylistRequest) -> str:
    ident, _ = await service.requests.create("owner", "explicit-key", spec)
    await service.process((await service.repo.runs(ident))[0]["id"])
    return ident


def test_the_resolved_tracks_publish_in_order_and_each_left_out_one_says_why(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            native = install(service)
            spec = request("Night Bloom", "Unheard Song", "Lantern Hall")
            ident = await published(service, spec)
            status = await service.requests.status("owner", ident)
            assert status["state"] == "published", status["error"]
            assert [e["id"] for e in native["entry"]] == ["n1", "n2"]
            assert [s["title"] for s in status["selected"]] == ["Night Bloom", "Lantern Hall"]
            assert status["resolution"] == {
                "requested": 3,
                "library": 2,
                "catalog": 0,
                "unresolved": 1,
            }
            first, missing, last = status["tracks"]
            assert first == {
                "position": 1,
                "input": {"artist": ARTIST, "title": "Night Bloom", "album": "", "version": ""},
                "outcome": "library",
                "song": {"artist": ARTIST, "title": "Night Bloom", "album": "Rooms"},
            }
            assert missing["position"] == 2 and missing["outcome"] == "unresolved"
            assert missing["reason"] == "not_found" and missing["detail"]
            assert missing["candidates"] == [
                {"artist": ARTIST, "title": "Unheard Song (Live)", "album": "Hall"}
            ]
            assert last["outcome"] == "library" and last["position"] == 3

    anyio.run(run)


def test_a_catalog_recording_and_a_repeated_one_are_reported(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            install(service)

            async def resolve(connection: Any, want: dict[str, Any], **kw: Any) -> Track | None:
                return CATALOG_SONG if want["title"] == "Far Signal" else SONGS["Night Bloom"]

            service.resolver.resolve = resolve  # type: ignore[method-assign]
            spec = PlaylistRequest(
                count=3,
                tracks=[
                    {"artist": ARTIST, "title": "Night Bloom"},
                    {"artist": "Orrin Vale", "title": "Far Signal"},
                    {"artist": ARTIST, "title": "Night Bloom", "album": ""},
                ],
                max_per_artist=2,
            )
            curated = await generate(service, connected(), spec.definition())
            assert [s.id for s in curated.selected] == ["n1", CATALOG_SONG.id]
            report = curated.curation["tracks"]
            assert [t["outcome"] for t in report] == ["library", "catalog", "unresolved"]
            assert report[2]["reason"] == "duplicate" and report[2]["same_as"] == 1

    anyio.run(run)


@pytest.mark.parametrize("require_all", [False, True])
def test_nothing_publishes_when_nothing_resolves_or_require_all_misses_one(
    tmp_path: Path, require_all: bool
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            native = install(service, missing="catalog_unavailable")
            titles = ("Night Bloom", "Unheard Song") if require_all else ("Unheard Song",)
            ident = await published(service, request(*titles, require_all=require_all))
            status = await service.requests.status("owner", ident)
            assert status["state"] == "failed" and not status["selected"] and not native
            assert ("require_all" in status["error"]) is require_all
            assert status["tracks"][-1]["reason"] == "catalog_unavailable"
            assert status["resolution"]["unresolved"] == 1

    anyio.run(run)


def test_require_all_is_an_explicit_track_option_and_keeps_old_replay_hashes() -> None:
    spec = request("Night Bloom")
    assert "require_all" not in json.loads(spec.canonical())
    assert json.loads(request("Night Bloom", require_all=True).canonical())["require_all"]
    with pytest.raises(ValidationError):
        PlaylistRequest(prompt="quiet", require_all=True)
    with pytest.raises(ValidationError):
        PlaylistRequest(count=1, tracks=[{"artist": "A", "title": "B"}], require_all="yes")


def test_added_tracks_take_the_left_out_places_and_republish_the_same_playlist(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            native = install(service)
            ident = await published(service, request("Night Bloom", "Unheard Song", "Lantern Hall"))
            first = await service.requests.status("owner", ident)
            assert first["state"] == "published" and native["id"] == "explicit-playlist"
            addition = TrackAddition(
                tracks=[
                    {"artist": "Mira Sole", "title": "Grey Tide", "replaces": 2},
                    {"artist": "Mira Sole", "title": "Paper Moths"},
                ]
            )
            assert await service.requests.add_tracks("owner", ident, "add-key-1", addition)
            # The same key and tracks again: nothing new.
            assert not await service.requests.add_tracks("owner", ident, "add-key-1", addition)
            run_id = (await service.repo.runs(ident))[0]["id"]
            await service.process(run_id)
            status = await service.requests.status("owner", ident)
            assert status["state"] == "published", status["error"]
            assert status["playlist_id"] == first["playlist_id"] == native["id"]
            assert [e["id"] for e in native["entry"]] == ["n1", "n3", "n2", "n4"]
            assert status["resolution"]["unresolved"] == 0 and status["count"] == 4
            assert [t["input"]["title"] for t in status["tracks"]] == [
                "Night Bloom",
                "Grey Tide",
                "Lantern Hall",
                "Paper Moths",
            ]
            assert not await service.requests.add_tracks("owner", ident, "add-key-1", addition)
            with pytest.raises(RequestError) as other:
                await service.requests.add_tracks(
                    "owner",
                    ident,
                    "add-key-1",
                    TrackAddition(tracks=[{"artist": "Orrin Vale", "title": "Far Signal"}]),
                )
            assert other.value.status == 409

    anyio.run(run)


def test_added_tracks_are_refused_where_they_would_change_too_much(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            native = install(service)
            ident = await published(service, request("Night Bloom", "Unheard Song"))

            async def refused(key: str, *tracks: dict[str, Any]) -> RequestError:
                with pytest.raises(RequestError) as caught:
                    await service.requests.add_tracks(
                        "owner", ident, key, TrackAddition(tracks=list(tracks))
                    )
                return caught.value

            resolved = await refused("add-key-1", {"artist": "A", "title": "B", "replaces": 1})
            assert "not a left-out track" in str(resolved)
            beyond = await refused("add-key-2", {"artist": "A", "title": "B", "replaces": 9})
            assert "not a left-out track" in str(beyond)
            artists = await refused("add-key-3", {"artist": ARTIST, "title": "Another"})
            assert "max_per_artist" in str(artists)
            with pytest.raises(ValidationError):
                TrackAddition(
                    tracks=[
                        {"artist": "A", "title": "B", "replaces": 2},
                        {"artist": "C", "title": "D", "replaces": 2},
                    ]
                )
            native["changed"] = "edited in Navidrome"
            edited = await refused("add-key-4", {"artist": "A", "title": "B", "replaces": 2})
            assert edited.status == 409 and str(edited) == EDITED
            assert len(await service.repo.runs(ident)) == 1

    anyio.run(run)


def test_tracks_added_after_an_edit_in_navidrome_never_overwrite_it(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            native = install(service)
            ident = await published(service, request("Night Bloom", "Unheard Song"))
            addition = TrackAddition(tracks=[{"artist": "Mira Sole", "title": "Grey Tide"}])
            assert await service.requests.add_tracks("owner", ident, "add-key-1", addition)
            native["changed"] = "edited while the addition waited"
            entries = list(native["entry"])
            await service.process((await service.repo.runs(ident))[0]["id"])
            status = await service.requests.status("owner", ident)
            assert status["state"] == "failed" and status["error"] == EDITED
            assert native["entry"] == entries

    anyio.run(run)


def test_a_request_whose_tracks_all_failed_takes_replacements(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            native = install(service)
            ident = await published(service, request("Unheard Song"))
            assert (await service.requests.status("owner", ident))["state"] == "failed"
            addition = TrackAddition(
                tracks=[{"artist": ARTIST, "title": "Night Bloom", "replaces": 1}]
            )
            assert await service.requests.add_tracks("owner", ident, "add-key-1", addition)
            await service.process((await service.repo.runs(ident))[0]["id"])
            status = await service.requests.status("owner", ident)
            assert status["state"] == "published", status["error"]
            assert [e["id"] for e in native["entry"]] == ["n1"]

    anyio.run(run)


def test_only_explicit_track_requests_take_added_tracks(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            ident, _ = await service.requests.create(
                "owner", "described-1", PlaylistRequest(prompt="quiet", count=1)
            )
            with pytest.raises(RequestError, match="explicit tracks"):
                await service.requests.add_tracks(
                    "owner",
                    ident,
                    "add-key-1",
                    TrackAddition(tracks=[{"artist": "A", "title": "B"}]),
                )

    anyio.run(run)


def test_unresolved_carries_its_report() -> None:
    error = Unresolved("message", [{"position": 1}])
    assert str(error) == "message" and error.tracks == [{"position": 1}]


def test_the_http_api_adds_tracks_under_an_idempotency_key(tmp_path: Path) -> None:
    import httpx
    from starlette.applications import Starlette

    from shijhon.playlists.api import API
    from shijhon.playlists.requests import digest
    from tests.harness.agents import agent_token

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            install(service)
            token = await agent_token(service, "owner", "Agent", history=False, library=False)
            ident, _ = await service.requests.create(
                "owner",
                "http-add-1",
                request("Night Bloom", "Unheard Song"),
                creator=digest(token),
                scopes=["create", "status"],
            )
            await service.process((await service.repo.runs(ident))[0]["id"])
            body = {"tracks": [{"artist": "Mira Sole", "title": "Grey Tide", "replaces": 2}]}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(Starlette(routes=API(lambda: service).routes())),
                base_url="https://test.invalid",
            ) as http:

                async def post(key: str, value: Any) -> httpx.Response:
                    return await http.post(
                        f"/api/playlists/requests/{ident}/tracks",
                        headers={"authorization": "Bearer " + token, "idempotency-key": key},
                        json=value,
                    )

                first = await post("http-add-key", body)
                assert first.status_code == 202, first.text
                assert first.json()["state"] == "queued"
                assert (await post("http-add-key", body)).status_code == 200
                assert (await post("http-add-key", {"tracks": []})).status_code == 400
                other = {"tracks": [{"artist": "Mira Sole", "title": "Paper Moths"}]}
                assert (await post("http-add-key", other)).status_code == 409

    anyio.run(run)


def test_a_track_the_catalog_did_not_answer_for_is_looked_up_again_a_few_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shijhon.playlists import prompt

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            install(service, missing="catalog_unavailable")
            pauses: list[float] = []

            async def pause(seconds: float) -> None:
                pauses.append(seconds)

            monkeypatch.setattr(prompt.anyio, "sleep", pause)
            monkeypatch.setattr(prompt, "RETRIES", 2)
            asked: list[str] = []

            async def resolve(connection: Any, want: dict[str, Any], **kw: Any) -> Track | None:
                asked.append(want["title"])
                # Night Bloom answers on its second look; the others never do.
                if want["title"] == "Night Bloom" and asked.count("Night Bloom") == 2:
                    return SONGS["Night Bloom"]
                kw["failures"].append("rate_limited")
                return None

            service.resolver.resolve = resolve  # type: ignore[method-assign]
            spec = request("Night Bloom", "Unheard Song", "Lantern Hall", "Grey Tide")
            curated = await generate(service, connected(), spec.definition())
            assert [s.id for s in curated.selected] == ["n1"]
            assert pauses == [prompt.RETRY_PAUSE] * 2
            assert asked == ["Night Bloom"] * 2 + ["Unheard Song"] * 2 + [
                "Lantern Hall",
                "Grey Tide",
            ]
            reasons = [t.get("reason") for t in curated.curation["tracks"]]
            assert reasons == [
                None,
                "catalog_unavailable",
                "catalog_unavailable",
                "catalog_unavailable",
            ]

    anyio.run(run)


@pytest.mark.parametrize(
    ("asked", "credited"),
    [
        ("Velane & The Lanterns", "Velane and The Lanterns"),
        ("Velane", "Velane feat. Ada Moss"),
        ("Velane", "Velane (feat. Ada Moss)"),
        ("Velane feat. Ada Moss", "Velane feat. Ada Moss & Orrin Vale"),
    ],
)
def test_an_explicit_track_s_credit_may_differ_in_featured_artists_and_and(
    asked: str, credited: str
) -> None:
    async def run() -> None:
        catalog = Catalog([("Rooms", "1998", [("c1", "Night Bloom", "")])], artist=credited)
        want = {
            **Suggestion(artist=asked, title="Night Bloom").wanted(),
            "strict_album": True,
            "prefer_original": True,
            "lenient_credit": True,
        }
        found = await Resolver(library(), catalog).resolve(connected(), want)  # type: ignore[arg-type]
        assert found is not None and found.artist == credited
        strict = {k: v for k, v in want.items() if k != "lenient_credit"}
        assert await Resolver(library(), catalog).resolve(connected(), strict) is None  # type: ignore[arg-type]

    anyio.run(run)


def test_a_named_featured_artist_must_be_credited_and_the_exact_credit_comes_first() -> None:
    def song(ident: str, artist: str, title: str = "Night Bloom") -> Track:
        return Track(id=ident, artist=artist, title=title, album="Rooms", year=2001)

    def want(artist: str) -> dict[str, Any]:
        return {**wanted("Night Bloom"), "artist": artist}

    solo, featuring = song("solo", ARTIST), song("feat", f"{ARTIST} feat. Ada Moss")
    found = choose(want(ARTIST), [featuring, solo])
    assert found is not None and found.id == "solo"
    found = choose(want(f"{ARTIST} feat. Ada Moss"), [solo, featuring])
    assert found is not None and found.id == "feat"
    assert choose(want(f"{ARTIST} feat. Ada Moss"), [solo]) is None
    titled = song("titled", ARTIST, "Night Bloom (feat. Ada Moss)")
    found = choose(want(f"{ARTIST} feat. Ada Moss"), [titled])
    assert found is not None and found.id == "titled"
    found = choose(want("M & M"), [song("m-and-m", "M and M")])
    assert found is not None
    found = choose(want("M&M"), [song("m-and-m", "M and M")])
    assert found is not None
    # A short featured name counts only as a whole word of the title.
    edited = song("x", ARTIST, "Night Bloom (Edited)")
    assert choose(want(f"{ARTIST} feat. Ed"), [edited]) is None
    with_ed = song("ed", ARTIST, "Night Bloom (with Ed)")
    assert choose(want(f"{ARTIST} feat. Ed"), [with_ed]) is not None


def test_a_credit_written_otherwise_is_found_in_the_library_by_its_title() -> None:
    async def run() -> None:
        native = library()
        owned_song = owned("n1", "Night Bloom", "Rooms", artist="Velane & The Lanterns")
        native.call.side_effect = [
            {"searchResult3": {"song": []}},  # "Velane and The Lanterns Night Bloom"
            {"searchResult3": {"song": [owned_song]}},  # "Night Bloom"
        ]
        want = {**wanted("Night Bloom"), "artist": "Velane and The Lanterns"}
        found = await Resolver(native, None).resolve(connected(), want)
        assert found is not None and found.id == "n1"
        queries = [dict(c.args[2])["query"] for c in native.call.await_args_list]
        assert queries == ["Velane and The Lanterns Night Bloom", "Night Bloom"]

    anyio.run(run)


def test_without_library_permission_the_report_does_not_tell_library_from_catalog(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            install(service)
            explained: list[bool] = []

            async def explain(connection: Any, want: dict[str, Any], **kw: Any) -> Explanation:
                explained.append(kw["library"])
                return Explanation("not_found", [])

            service.resolver.explain = explain  # type: ignore[method-assign]
            spec = request("Night Bloom", "Unheard Song")
            library_reads = AsyncMock(return_value=False)
            curated = await generate(service, connected(), spec.definition(), library=library_reads)
            report = curated.curation["tracks"]
            assert [t["outcome"] for t in report] == ["resolved", "unresolved"]
            assert explained == [False]
            assert summary(report) == {"requested": 2, "resolved": 1, "unresolved": 1}
            library_reads.return_value = True
            curated = await generate(service, connected(), spec.definition(), library=library_reads)
            assert [t["outcome"] for t in curated.curation["tracks"]] == ["library", "unresolved"]
            assert explained == [False, True]

    anyio.run(run)


def test_tracks_not_looked_up_in_the_run_s_time_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shijhon.playlists import prompt

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            install(service)
            clock = iter([0.0, 1.0, 2.0, 10_000.0])
            monkeypatch.setattr(prompt, "clock", lambda: next(clock))
            spec = request("Night Bloom", "Lantern Hall", "Grey Tide")
            curated = await generate(service, connected(), spec.definition())
            assert [s.id for s in curated.selected] == ["n1", "n2"]
            last = curated.curation["tracks"][-1]
            assert last["reason"] == "not_checked" and last["outcome"] == "unresolved"

    anyio.run(run)


def test_added_tracks_keep_the_published_ones_without_looking_them_up_again(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            native = install(service)
            ident = await published(service, request("Night Bloom", "Unheard Song", "Lantern Hall"))
            asked: list[str] = []
            first = service.resolver.resolve

            async def resolve(connection: Any, want: dict[str, Any], **kw: Any) -> Track | None:
                asked.append(want["title"])
                return await first(connection, want, **kw)

            service.resolver.resolve = resolve  # type: ignore[method-assign]
            addition = TrackAddition(
                tracks=[{"artist": "Mira Sole", "title": "Grey Tide", "replaces": 2}]
            )
            assert await service.requests.add_tracks("owner", ident, "add-key-1", addition)
            await service.process((await service.repo.runs(ident))[0]["id"])
            status = await service.requests.status("owner", ident)
            assert status["state"] == "published", status["error"]
            assert asked == ["Grey Tide"]
            assert [e["id"] for e in native["entry"]] == ["n1", "n3", "n2"]
            assert [t["outcome"] for t in status["tracks"]] == ["library"] * 3

    anyio.run(run)


def test_a_discarded_or_failed_addition_still_takes_further_additions(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            install(service)
            ident = await published(service, request("Night Bloom", "Unheard Song"))
            addition = TrackAddition(tracks=[{"artist": "Mira Sole", "title": "Grey Tide"}])
            assert await service.requests.add_tracks("owner", ident, "add-key-1", addition)
            queued = (await service.repo.runs(ident))[0]
            await service.repo.update(queued["id"], "discarded", queued["data"])
            again = TrackAddition(
                tracks=[{"artist": "Orrin Vale", "title": "Lantern Hall", "replaces": 2}]
            )
            assert await service.requests.add_tracks("owner", ident, "add-key-2", again)

    anyio.run(run)


def test_status_hides_library_outcomes_from_a_requester_without_library_permission(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            install(service)
            ident = await published(service, request("Night Bloom", "Unheard Song"))
            first = await service.requests.status("owner", ident)
            assert first["tracks"][0]["outcome"] == "library"
            service.requests.reads_library = AsyncMock(return_value=False)  # type: ignore[method-assign]
            status = await service.requests.status("owner", ident)
            assert [t["outcome"] for t in status["tracks"]] == ["resolved", "unresolved"]
            assert status["resolution"] == {"requested": 2, "resolved": 1, "unresolved": 1}

    anyio.run(run)


def test_a_track_the_library_refuses_for_its_album_is_left_out_at_publication(
    tmp_path: Path,
) -> None:
    """An owned album with a settled Library decision refuses a catalog song: that
    track is left out and reported, and the others publish."""
    from contextlib import nullcontext

    from shijhon.views.commits import CommitError

    refused_id = song_id(CatalogRef("demo", "c1"))
    other_id = song_id(CatalogRef("demo", "c2"))

    def catalog_song(own: str, title: str) -> CatalogTrack:
        return CatalogTrack(
            CatalogRef("demo", own), title, ARTIST, 200_000, album=CatalogRef("demo", "al1")
        )

    songs = {"c1": catalog_song("c1", "Night Bloom"), "c2": catalog_song("c2", "Grey Tide")}

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            native = install(service)
            asked: list[set[str]] = []

            async def natives(ids: set[str], **kw: Any) -> dict[str, str]:
                asked.append(set(ids))
                if refused_id in ids:
                    raise CommitError(0, "private", category="owned_decision", item=refused_id)
                return {other_id: "n3"}

            async def song(ident: str) -> CatalogTrack:
                return songs[ident]

            async def album(ident: str) -> CatalogRelease:
                return CatalogRelease(
                    CatalogRef("demo", ident), "Rooms", ARTIST, ReleaseKind.ALBUM, "1998",
                    tracks=tuple(songs.values()),
                )  # fmt: skip

            service.catalog = SimpleNamespace(key="demo", song=song, album=album)  # type: ignore[assignment]
            service.commits = SimpleNamespace(  # type: ignore[assignment]
                engine=SimpleNamespace(
                    guard_materializations=lambda check: nullcontext(),
                    guard_writes=lambda authorize: nullcontext(),
                ),
                natives=natives,
                owned=None,
            )
            by_title = {
                "Night Bloom": Track(
                    id=refused_id, artist=ARTIST, title="Night Bloom", album="Rooms",
                    duration=200, release="demo:al1",
                ),
                "Grey Tide": Track(
                    id=other_id, artist=ARTIST, title="Grey Tide", album="Rooms",
                    duration=200, release="demo:al1",
                ),
                "Lantern Hall": SONGS["Lantern Hall"],
            }  # fmt: skip

            async def resolve(connection: Any, want: dict[str, Any], **kw: Any) -> Track | None:
                return by_title.get(want["title"])

            service.resolver.resolve = resolve  # type: ignore[method-assign]
            original = service.client.call

            async def call(connection: Any, method: str, params: Any = ()) -> Any:
                if method == "getSong" and dict(params)["id"] == "n3":
                    grey = by_title["Grey Tide"].model_copy(update={"id": "n3", "release": ""})
                    return {"song": grey.model_dump()}
                return await original(connection, method, params)

            service.client.call = call  # type: ignore[method-assign]
            spec = PlaylistRequest(
                name="Lantern evening",
                count=3,
                tracks=[
                    {"artist": ARTIST, "title": "Night Bloom"},
                    {"artist": ARTIST, "title": "Grey Tide"},
                    {"artist": "Orrin Vale", "title": "Lantern Hall"},
                ],
            )
            ident = await published(service, spec)
            status = await service.requests.status("owner", ident)
            assert status["state"] == "published", status["error"]
            assert [e["id"] for e in native["entry"]] == ["n3", "n2"]
            assert asked == [{refused_id, other_id}, {other_id}]
            first = status["tracks"][0]
            assert first["outcome"] == "unresolved" and first["reason"] == "library_refused"
            assert "settled Library decision" in first["detail"] and "song" not in first
            assert status["resolution"] == {
                "requested": 3,
                "library": 1,
                "catalog": 1,
                "unresolved": 1,
            }
            assert [s["title"] for s in status["selected"]] == ["Grey Tide", "Lantern Hall"]
            service.requests.reads_library = AsyncMock(return_value=False)  # type: ignore[method-assign]
            hidden = (await service.requests.status("owner", ident))["tracks"][0]
            assert hidden["reason"] == "not_added" and "Library" not in hidden["detail"]

    anyio.run(run)


def test_a_refused_track_still_fails_the_selection_when_nothing_else_would_stay(
    tmp_path: Path,
) -> None:
    from contextlib import nullcontext

    from shijhon.playlists.model import PlaylistError
    from shijhon.views.commits import CommitError

    refused_id = song_id(CatalogRef("demo", "c1"))
    catalog_track = CatalogTrack(
        CatalogRef("demo", "c1"), "Night Bloom", ARTIST, 200_000, album=CatalogRef("demo", "al1")
    )

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:

            async def natives(ids: set[str], **kw: Any) -> dict[str, str]:
                raise CommitError(0, "private", category="owned_decision", item=refused_id)

            async def song(ident: str) -> CatalogTrack:
                return catalog_track

            service.catalog = SimpleNamespace(key="demo", song=song)  # type: ignore[assignment]
            service.commits = SimpleNamespace(  # type: ignore[assignment]
                engine=SimpleNamespace(guard_materializations=lambda check: nullcontext()),
                natives=natives,
            )
            only = Track(
                id=refused_id, artist=ARTIST, title="Night Bloom", duration=200, release="demo:al1"
            )
            left: dict[str, str] = {}
            with pytest.raises(PlaylistError, match="settled Library decision"):
                await service.check_tracks(connected(), [only], {}, leave_out=left)
            assert left == {}

    anyio.run(run)
