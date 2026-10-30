"""Imports also ask the catalogs of the add-ons: for a recording that neither the
library nor the catalog has, in the add-ons' order; from the live list of add-ons; and
its release is added from that catalog when the reviewed import is published - also
after a restart, when the add-on's catalog remembers none of its songs. Every name,
title and ID is invented."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from shijhon.catalog.addon import AddonCatalogs
from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.delivery.addon import Addon, catalog_key, tagged
from shijhon.placeholders.engine import MaterializeResult
from shijhon.playlists.model import Definition, PlaylistError, Suggestion, Track
from shijhon.playlists.resolve import Resolver
from shijhon.views.commits import CommitError, Commits
from tests.harness.addon_catalog import FakeCatalog, Record, Song
from tests.harness.library import cover_image
from tests.unit.test_addon_catalog import BASE, SERVICE, Listed, Registry, Server
from tests.unit.test_playlist_import_batches import Native, process_until
from tests.unit.test_playlist_requests import connected, service_at

KEY = catalog_key("Fake")


def wanted(artist: str, title: str, album: str = "") -> dict[str, Any]:
    return Suggestion(artist=artist, title=title, album=album).wanted()


def client(songs: list[dict[str, Any]] | None = None) -> AsyncMock:
    """The owner's Navidrome: its library has ``songs``."""
    native = AsyncMock()
    native.native.library_id = 1
    native.call.return_value = {"searchResult3": {"song": songs or []}}
    return native


@dataclass
class Stub:
    """A catalog with a few invented songs, each on a release of its own."""

    key: str
    songs: list[CatalogTrack] = field(default_factory=list)
    region: str = ""
    asked: list[str] = field(default_factory=list)
    incomplete: bool = False

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        self.asked.append(term)
        return SearchResults(songs=tuple(self.songs))

    async def song(self, ident: str) -> CatalogTrack:
        return next(s for s in self.songs if s.ref.id == ident)

    async def album(self, ident: str) -> CatalogRelease:
        listed = tuple(s for s in self.songs if s.album and s.album.id == ident)
        if not listed:
            raise CatalogError("not_found", "no such album")
        return CatalogRelease(
            CatalogRef(self.key, ident),
            "Invented Rooms",
            listed[0].artist,
            ReleaseKind.ALBUM,
            "2020",
            tracks=listed,
            incomplete=self.incomplete,
        )


def song(key: str, own: str, title: str, seconds: int = 200, album: str = "") -> CatalogTrack:
    return CatalogTrack(
        CatalogRef(key, own),
        title,
        "Velane",
        seconds * 1000,
        album=CatalogRef(key, album or f"al{own}"),
        album_title="Invented Rooms",
    )


def addons(*catalogs: Stub) -> SimpleNamespace:
    listed = [(f"Add-on {c.key}", c) for c in catalogs]

    async def asked(
        unanswered: set[str] | None = None, unreadable: set[str] | None = None
    ) -> list[tuple[str, Stub]]:
        passed = (unanswered or set()) | (unreadable or set())
        return [(name, c) for name, c in listed if name not in passed]

    return SimpleNamespace(catalogs=AsyncMock(side_effect=asked))


def resolve(resolver: Resolver, artist: str, title: str, **kwargs: Any) -> Any:
    ambiguous: list[Track] = []

    async def run() -> Track | None:
        return await resolver.resolve(
            connected(), wanted(artist, title), ambiguous=ambiguous, **kwargs
        )

    return anyio.run(run), ambiguous


# --- the order ------------------------------------------------------------------------------------


def test_add_on_catalogs_are_asked_in_order_for_what_library_and_catalog_lack() -> None:
    main = Stub("demo")
    first, second = Stub("addonfirst"), Stub("addonsecond")
    second.songs = [song("addonsecond", "s1", "Glass Orchard")]
    resolver = Resolver(client(), main, addons(first, second))  # type: ignore[arg-type]
    found, ambiguous = resolve(resolver, "Velane", "Glass Orchard", addons=True)
    assert found is not None and not ambiguous
    assert found.id == "sh.tr.addonsecond.s1" and found.release == "addonsecond:als1"
    assert found.album == "Invented Rooms"
    assert main.asked == first.asked == second.asked == ["Velane Glass Orchard"]
    # The catalog has it: no add-on is asked.
    main.songs = [song("demo", "d1", "Glass Orchard")]
    first.asked.clear()
    found, _ = resolve(resolver, "Velane", "Glass Orchard", addons=True)
    assert found is not None and found.id == "sh.tr.demo.d1" and first.asked == []
    # Nor for what the library has.
    owned = [{"id": "native-1", "artist": "Velane", "title": "Glass Orchard"}]
    found, _ = resolve(
        Resolver(client(owned), Stub("demo"), addons(first)),  # type: ignore[arg-type]
        "Velane",
        "Glass Orchard",
        addons=True,
    )
    assert found is not None and found.id == "native-1" and first.asked == []


def test_several_candidates_decide_like_the_catalog_s() -> None:
    """Candidates the library or the catalog cannot tell apart make the entry ambiguous
    there; the first add-on's candidates make it ambiguous with those - the next add-on is
    not asked."""
    main, first, second = Stub("demo"), Stub("addonfirst"), Stub("addonsecond")
    main.songs = [song("demo", "d1", "Harbor"), song("demo", "d2", "Harbor", album="other")]
    second.songs = [song("addonsecond", "s1", "Harbor")]
    resolver = Resolver(client(), main, addons(first, second))  # type: ignore[arg-type]
    found, ambiguous = resolve(resolver, "Velane", "Harbor", addons=True)
    assert found is None and [s.id for s in ambiguous] == ["sh.tr.demo.d1", "sh.tr.demo.d2"]
    assert first.asked == second.asked == []
    main.songs = []
    first.songs = [
        song("addonfirst", "f1", "Harbor"),
        song("addonfirst", "f2", "Harbor", album="other"),
    ]
    found, ambiguous = resolve(resolver, "Velane", "Harbor", addons=True)
    assert found is None and {s.id for s in ambiguous} == {
        "sh.tr.addonfirst.f1",
        "sh.tr.addonfirst.f2",
    }
    assert second.asked == []


def test_an_add_on_s_incomplete_release_or_failure_leaves_the_next_add_on() -> None:
    first, second = Stub("addonfirst", incomplete=True), Stub("addonsecond")
    first.songs = [song("addonfirst", "f1", "Low Tide")]
    second.songs = [song("addonsecond", "s1", "Low Tide")]
    resolver = Resolver(client(), None, addons(first, second))  # type: ignore[arg-type]
    found, _ = resolve(resolver, "Velane", "Low Tide", addons=True)
    assert found is not None and found.id == "sh.tr.addonsecond.s1"

    async def failing(term: str, limit: int = 20) -> SearchResults:
        raise CatalogError("unavailable", "no answer in time")

    first.search = failing  # type: ignore[method-assign]
    found, _ = resolve(resolver, "Velane", "Low Tide", addons=True)
    assert found is not None and found.id == "sh.tr.addonsecond.s1"
    # Nothing anywhere: unresolved, without candidates.
    found, ambiguous = resolve(resolver, "Velane", "Absent", addons=True)
    assert found is None and ambiguous == []


def test_only_imports_ask_the_add_ons() -> None:
    """Described playlists and discovery (no ``addons``) stay with the catalog."""
    first = Stub("addonfirst", songs=[song("addonfirst", "f1", "Glass Orchard")])
    stubs = addons(first)
    resolver = Resolver(client(), Stub("demo"), stubs)  # type: ignore[arg-type]
    assert resolve(resolver, "Velane", "Glass Orchard")[0] is None
    found = anyio.run(lambda: resolver.resolve(connected(), wanted("Velane", "Glass Orchard")))
    assert found is None
    stubs.catalogs.assert_not_awaited()
    assert first.asked == []


# --- the live list of add-ons ---------------------------------------------------------------------


@dataclass
class Installed:
    """Two add-ons behind mock transports: "Fake" with a catalog, "Plain" without."""

    registry: Registry
    fake: Server
    plain: Server
    sources: list[Listed]


@pytest.fixture
def installed() -> Iterator[Installed]:
    fake = Server(FakeCatalog("plain", prefix="lib:"))
    plain = Server(FakeCatalog(), ("stream", "isrc"))
    plain.service = "test.plain"
    sources = [
        Listed(1, "Plain", Addon(BASE, None, httpx.AsyncClient(transport=transport(plain)))),
        Listed(2, "Fake", Addon(BASE, None, httpx.AsyncClient(transport=transport(fake)))),
    ]
    yield Installed(Registry(list(sources)), fake, plain, sources)


def transport(server: Server) -> httpx.MockTransport:
    return httpx.MockTransport(server.handle)


def own(item: str) -> str:
    return f"{KEY}:{tagged(SERVICE, 'lib:' + item)}"


def test_the_catalogs_are_those_of_the_enabled_add_ons_that_declare_one(
    installed: Installed,
) -> None:
    async def run() -> None:
        catalogs = AddonCatalogs(installed.registry)  # type: ignore[arg-type]
        try:
            found = await catalogs.catalogs()
            assert [(name, c.key) for name, c in found] == [("Fake", KEY)]
            assert installed.plain.asked == ["manifest.json"]  # never searched
            # One catalog for each add-on: what it was shown is remembered.
            assert await catalogs.by_key(KEY) is found[0][1]
            assert catalogs.name(KEY) == "Fake" and catalogs.name("demo") is None
            # Switched off in the dashboard: not asked from then on, without a restart.
            installed.registry.sources = [installed.sources[0]]
            assert await catalogs.catalogs() == [] and await catalogs.by_key(KEY) is None
            installed.registry.sources = list(installed.sources)
            assert [name for name, _ in await catalogs.catalogs()] == ["Fake"]
            # The add-on that is the catalog is asked as that one.
            skipping = AddonCatalogs(installed.registry, skip="Fake")  # type: ignore[arg-type]
            assert await skipping.catalogs() == [] and await skipping.by_key(KEY) is None
        finally:
            await catalogs.aclose()

    anyio.run(run)


def test_a_song_without_its_release_is_found_and_added_after_a_restart(
    installed: Installed, tmp_path: Path
) -> None:
    """A search names a song's album by title only: its release is looked up. The
    reviewed release is published from the add-on's catalog, also when its memory of
    songs is gone, with the add-on's catalog given to the commit - only then."""

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            catalogs = AddonCatalogs(installed.registry)  # type: ignore[arg-type]
            service.addons = catalogs
            service.resolver = Resolver(service.client, None, catalogs)
            service.client.call = AsyncMock(return_value={"searchResult3": {"song": []}})
            ambiguous: list[Track] = []
            found = await service.resolver.resolve(
                connected(), wanted("Ines Calder", "Kite Season"), ambiguous=ambiguous, addons=True
            )
            assert found is not None and ambiguous == []
            assert found.id == f"sh.tr.{KEY}.{own('t502').split(':')[1]}"
            assert found.release == own("al5") and found.album == "Paper Lanterns"
            assert service.imports.addon(found.id) == "Fake"
            assert service.imports.addon("sh.tr.demo.x") is None
            frozen = await service.freeze(
                Definition(mode="import", count=1, cadence="manual", review_before_publishing=True),
                [found],
                "",
                "",
                history_from=0,
                history_count=0,
                skipped=0,
                addons=True,
            )
            (release,) = frozen["releases"]
            assert release["ref"] == own("al5") and release["tracks"] == 3
            # Owned album plans are the catalog's: an add-on's release is not substituted.
            main = SimpleNamespace(key="demo", album=AsyncMock())
            plans = SimpleNamespace(matched_release=AsyncMock())
            service.catalog = main  # type: ignore[assignment]
            service.commits = SimpleNamespace(owned=plans)  # type: ignore[assignment]
            assert await service.matched_edition(found) == found
            main.album.assert_not_awaited()
            plans.matched_release.assert_not_awaited()
            # Described playlists do not add an add-on's releases.
            service.commits = None
            with pytest.raises(PlaylistError, match="unavailable"):
                await service.check_tracks(connected(), [found], {})
            # A restart: nothing of the add-on's catalog is remembered.
            await catalogs.aclose()
            given: list[dict[str, Any]] = []

            async def natives(ids: set[str], **kwargs: Any) -> dict[str, str]:
                given.append(kwargs["catalogs"])
                (catalog,) = kwargs["catalogs"].values()
                # The song is known again: the commit asks for it by its ID.
                assert (await catalog.song(CatalogRef.parse(own("t502")).id)).title == (
                    "Kite Season"
                )
                return {found.id: "native-502"}

            service.commits = SimpleNamespace(  # type: ignore[assignment]
                natives=natives,
                engine=SimpleNamespace(guard_materializations=lambda check: nullcontext()),
            )
            albums = installed.fake.count("album")
            service.client.call = AsyncMock(
                return_value={
                    "song": {
                        "id": "native-502",
                        "artist": "Ines Calder",
                        "title": "Kite Season",
                        "duration": 5,
                        "isrc": "ZZSHA0000502",
                    }
                }
            )
            ids = await service.check_tracks(
                connected(), [found], {release["ref"]: release["fingerprint"]}, addons=True
            )
            assert ids == ["native-502"] and [list(g) for g in given] == [[KEY]]
            assert installed.fake.count("album") == albums + 1
            await catalogs.aclose()

    anyio.run(run)


# --- the commit -----------------------------------------------------------------------------------


def test_a_commit_adds_an_add_on_s_release_only_when_given_its_catalog() -> None:
    """Clients' requests keep to the catalog. An import's release of an add-on's catalog
    is a catalog album of its own - never filled into an owned album - with that
    catalog's cover."""
    jpeg = cover_image("blue").read_bytes()
    track = CatalogTrack(
        CatalogRef(KEY, "t1"),
        "Glass Orchard",
        "Velane",
        200_000,
        album=CatalogRef(KEY, "al1"),
    )
    release = CatalogRelease(
        CatalogRef(KEY, "al1"),
        "Invented Rooms",
        "Velane",
        ReleaseKind.ALBUM,
        "2020",
        tracks=(track,),
        artwork_template="https://images.example.invalid/al1.jpg",
    )
    foreign = SimpleNamespace(
        key=KEY,
        song=AsyncMock(return_value=track),
        album=AsyncMock(return_value=release),
        artwork=AsyncMock(return_value=(jpeg, "image/jpeg")),
    )
    main = SimpleNamespace(key="demo", region="", artwork=AsyncMock())
    linked: dict[str, str] = {}

    async def linked_song(ref: CatalogRef) -> str | None:
        return linked.get(str(ref))

    engine = SimpleNamespace(materialize=AsyncMock())

    async def materialize(release: CatalogRelease, **kwargs: Any) -> MaterializeResult:
        linked[str(track.ref)] = "native-1"
        return MaterializeResult(release.ref, "native-album", {track.ref: "native-1"})

    engine.materialize.side_effect = materialize
    commits = Commits(
        main,  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(song=linked_song, album=AsyncMock(return_value=None)),  # type: ignore[arg-type]
    )
    commits.owned = SimpleNamespace(saved_release=AsyncMock(), first_use=AsyncMock())  # type: ignore[assignment]

    async def run() -> None:
        with pytest.raises(CommitError) as refused:
            await commits.natives({f"sh.tr.{KEY}.t1"})
        assert refused.value.message == "catalog item not found"
        foreign.song.assert_not_awaited()
        mapping = await commits.natives({f"sh.tr.{KEY}.t1"}, catalogs={KEY: foreign})  # type: ignore[dict-item]
        assert mapping == {f"sh.tr.{KEY}.t1": "native-1"}
        commits.owned.saved_release.assert_not_awaited()  # type: ignore[union-attr]
        commits.owned.first_use.assert_not_awaited()  # type: ignore[union-attr]
        assert engine.materialize.await_args.kwargs["cover"] == jpeg
        main.artwork.assert_not_awaited()

    anyio.run(run)


def test_an_add_on_s_candidate_is_chosen_after_its_catalog_forgot_it(
    installed: Installed, tmp_path: Path
) -> None:
    """Two recordings of the add-on's catalog match: the entry is ambiguous with both.
    The owner's choice is checked with the add-on - also once its catalog has forgotten
    the songs its search showed (as after a restart)."""
    live = Record(
        "al90", "Live Rooms", "Ines Calder", 2024, [Song("t901", "Kite Season", "Ines Calder", 5)]
    )
    installed.fake.held.records.append(live)

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            catalogs = AddonCatalogs(installed.registry)  # type: ignore[arg-type]
            service.addons = catalogs
            service.resolver = Resolver(service.client, None, catalogs)
            service.client.call = AsyncMock(return_value={"searchResult3": {"song": []}})
            ident = await service.imports.create(
                "owner", "choose-key", "Chosen", "Ines Calder — Kite Season"
            )
            run = (await service.repo.runs(ident))[0]["id"]
            preview = await process_until(service, run, "preview")
            (row,) = preview["data"]["rows"]
            assert row["state"] == "ambiguous"
            assert row["note"] == (
                "Several recordings match in the catalog of add-on Fake."
                " Choose the requested recording."
            )
            choice = next(c["track"] for c in row["choices"] if "t901" in c["track"]["id"])
            assert choice["release"] == ""  # (a search names its album by title only)
            await catalogs.aclose()
            await service.imports.action(
                "owner",
                ident,
                run,
                "choose",
                "",
                service.imports.review_key(preview["data"]),
                entry="1",
                choice=choice["id"],
            )
            chosen = await process_until(service, run, "preview")
            (row,) = chosen["data"]["rows"]
            assert row["state"] == "matched" and row["note"] == "Recording chosen explicitly"
            assert row["track"]["release"] == own("al90") and row["track"]["album"] == "Live Rooms"
            assert [r["ref"] for r in chosen["data"]["releases"]] == [own("al90")]
            await catalogs.aclose()

    anyio.run(run)


# --- slow catalogs --------------------------------------------------------------------------------


def test_a_slow_matching_batch_stops_in_time_and_keeps_its_matches(tmp_path: Path) -> None:
    """A batch stops before the run's time is used up: its matches are saved and the
    next batch goes on from there."""

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            service.settings.run_seconds = 3.0  # (a batch: half of it)

            async def slow(connection: Any, wanted: dict[str, Any], **kwargs: Any) -> Track:
                await anyio.sleep(0.6)
                return Track(id=f"native-{wanted['title']}", artist="A", title=wanted["title"])

            service.resolver.resolve = AsyncMock(side_effect=slow)
            lines = "\n".join(f"A — Song {n}" for n in range(5))
            ident = await service.imports.create("owner", "slow-key", "Slow", lines)
            run = (await service.repo.runs(ident))[0]["id"]
            await service.process(run)
            first = await service.repo.run(run)
            assert first["state"] == "queued" and 1 < first["data"]["cursor"] < 5
            assert len(first["data"]["rows"]) == first["data"]["cursor"]
            done = await process_until(service, run, "preview")
            assert [s["id"] for s in done["data"]["selected"]] == [
                f"native-Song {n}" for n in range(5)
            ]
            assert service.resolver.resolve.await_count == 5  # nothing matched twice

    anyio.run(run)


# --- add-ons that do not answer -------------------------------------------------------------------


def test_an_add_on_that_did_not_answer_is_skipped_not_taken_as_lacking_the_song() -> None:
    """Busy (rate limited) or unavailable is no answer: the add-on is remembered as
    unanswered for the attempt and not asked again; "not found" is an answer."""
    first, second = Stub("addonfirst"), Stub("addonsecond")
    second.songs = [song("addonsecond", "s1", "Low Tide")]
    asked: list[str] = []

    async def busy(term: str, limit: int = 20) -> SearchResults:
        asked.append(term)
        raise CatalogError("rate_limited", "not asked: the add-on is paused")

    first.search = busy  # type: ignore[method-assign]
    resolver = Resolver(client(), None, addons(first, second))  # type: ignore[arg-type]
    unanswered: set[str] = set()
    found, _ = resolve(resolver, "Velane", "Low Tide", addons=True, unanswered=unanswered)
    assert found is not None and found.id == "sh.tr.addonsecond.s1"
    assert unanswered == {"Add-on addonfirst"}
    found, ambiguous = resolve(resolver, "Velane", "Absent", addons=True, unanswered=unanswered)
    assert found is None and ambiguous == []
    assert asked == ["Velane Low Tide"]  # not asked again in this attempt
    assert second.asked == ["Velane Low Tide", "Velane Absent"]

    # Its release could not be read either: no answer.
    first.search = Stub.search.__get__(first)  # type: ignore[method-assign]
    first.songs = [song("addonfirst", "f1", "Low Tide")]

    async def unavailable(ident: str) -> CatalogRelease:
        raise CatalogError("unavailable", "no answer in time")

    first.album = unavailable  # type: ignore[method-assign]
    fresh: set[str] = set()
    found, _ = resolve(resolver, "Velane", "Low Tide", addons=True, unanswered=fresh)
    assert found is not None and found.id == "sh.tr.addonsecond.s1"
    assert fresh == {"Add-on addonfirst"}

    async def lacking(term: str, limit: int = 20) -> SearchResults:
        raise CatalogError("not_found", "nothing")

    first.search = lacking  # type: ignore[method-assign]
    answered: set[str] = set()
    resolve(resolver, "Velane", "Low Tide", addons=True, unanswered=answered)
    assert answered == set()


def test_entries_say_when_an_add_on_did_not_answer_until_a_new_attempt(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:

            async def silent(
                connection: Any, wanted: dict[str, Any], **kwargs: Any
            ) -> Track | None:
                kwargs["unanswered"].add("Slow Bot")
                return None

            service.resolver.resolve = AsyncMock(side_effect=silent)
            ident = await service.imports.create("owner", "busy-key", "Busy", "A — Song")
            run = (await service.repo.runs(ident))[0]["id"]
            done = await process_until(service, run, "preview")
            [row] = done["data"]["rows"]
            assert row["state"] == "unresolved"
            assert row["note"] == "Not found yet: add-on Slow Bot did not answer. Recheck later."
            assert done["data"]["unanswered"] == ["Slow Bot"]
            key = service.imports.review_key(done["data"])
            await service.imports.action("owner", ident, run, "retry", "", key)
            retried = await service.repo.run(run)
            assert "unanswered" not in retried["data"]  # a new attempt asks it again

    anyio.run(run)


def test_a_slow_add_on_s_own_time_budget_bounds_its_catalog_requests(
    installed: Installed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        catalogs = AddonCatalogs(installed.registry, timeout=0.1)  # type: ignore[arg-type]
        try:
            [(_, first)] = await catalogs.catalogs()
            assert first.inner.timeout == 0.1  # type: ignore[attr-defined]
            closed = anyio.Event()
            close = first.aclose

            async def close_first() -> None:
                await close()
                closed.set()

            monkeypatch.setattr(first, "aclose", close_first)
            installed.sources[1].budget = 170.0
            [(_, slow)] = await catalogs.catalogs()
            assert slow is not first and slow.inner.timeout == 170.0  # type: ignore[attr-defined]
            assert await catalogs.by_key(KEY) is slow
            await anyio.lowlevel.checkpoint()
            assert not closed.is_set()
            assert not first.inner.http.is_closed  # type: ignore[attr-defined]
            with anyio.fail_after(2):
                await closed.wait()
            assert first.inner.http.is_closed  # type: ignore[attr-defined]
            assert not slow.inner.http.is_closed  # type: ignore[attr-defined]
        finally:
            await catalogs.aclose()

    anyio.run(run)


def test_waiting_runs_take_turns_so_a_slow_import_holds_up_no_other(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            service.settings.run_seconds = 1.0  # (a batch: half of it)
            order: list[str] = []

            async def slow(connection: Any, wanted: dict[str, Any], **kwargs: Any) -> Track:
                order.append(wanted["title"])
                await anyio.sleep(0.6)
                return Track(id=f"native-{wanted['title']}", artist="A", title=wanted["title"])

            service.resolver.resolve = AsyncMock(side_effect=slow)
            first = await service.imports.create("owner", "turn-one", "One", "A — One 1\nA — One 2")
            second = await service.imports.create(
                "owner", "turn-two", "Two", "A — Two 1\nA — Two 2"
            )
            await service.tick()
            # One batch each in turn, not the whole first import before the second.
            assert order == ["One 1", "Two 1", "One 2", "Two 2"]
            for ident in (first, second):
                assert (await service.repo.runs(ident))[0]["state"] == "preview"

    anyio.run(run)


def test_an_add_on_whose_manifest_cannot_be_read_did_not_answer(installed: Installed) -> None:
    async def run() -> None:
        catalogs = AddonCatalogs(installed.registry)  # type: ignore[arg-type]
        try:
            installed.fake.status["manifest.json"] = httpx.Response(503)
            resolver = Resolver(client(), None, catalogs)  # type: ignore[arg-type]
            unanswered: set[str] = set()
            unreadable: set[str] = set()
            for title in ("Low Tide", "High Tide"):
                found = await resolver.resolve(
                    connected(),
                    wanted("Velane", title),
                    ambiguous=[],
                    addons=True,
                    unanswered=unanswered,
                    unreadable=unreadable,
                )
                assert found is None
            assert unanswered == {"Fake"} and unreadable == set()
            assert installed.fake.count("manifest.json") == 1  # not asked again
            # A refused manifest is not "no answer": rechecking later does not help.
            fresh = AddonCatalogs(installed.registry)  # type: ignore[arg-type]
            try:
                installed.fake.status["manifest.json"] = httpx.Response(403)
                refused: set[str] = set()
                broken: set[str] = set()
                assert await fresh.catalogs(refused, broken) == []
                assert refused == set() and broken == {"Fake"}
                assert await fresh.catalogs(refused, broken) == []
                assert installed.fake.count("manifest.json") == 2  # once for this attempt
            finally:
                await fresh.aclose()
        finally:
            await catalogs.aclose()

    anyio.run(run)


class Titled(Native):
    """The synthetic Navidrome, with "B — Song <n>" for native ID "native-<n>"."""

    @staticmethod
    def song(ident: str) -> dict[str, str]:
        return {"id": ident, "artist": "B", "title": f"Song {ident.split('-')[1]}"}


def test_slow_release_checks_still_prepare_something_every_turn(tmp_path: Path) -> None:
    """Publication checks each reviewed release again before adding it. With a slow
    catalog a turn ends in time: it prepares the songs whose releases it checked, saves
    them, and the next turn goes on from there; nothing is checked twice."""

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            asked: list[str] = []
            slow = Stub("demo")

            async def album(ident: str) -> CatalogRelease:
                asked.append(ident)
                await anyio.sleep(0.04)
                n = ident.removeprefix("al")
                track = song("demo", f"t{n}", f"Song {n}", album=ident)
                return CatalogRelease(
                    CatalogRef("demo", ident), f"Release {n}", "B", ReleaseKind.ALBUM, "2020",
                    tracks=(track,),
                )  # fmt: skip

            slow.album = album  # type: ignore[method-assign]

            async def songs_by_isrc(isrc: str) -> tuple[CatalogTrack, ...]:
                return ()

            slow.songs_by_isrc = songs_by_isrc  # type: ignore[attr-defined]
            cached = CachedCatalog(slow, ttl=3600)  # type: ignore[arg-type]

            async def catalog_of(key: str, *, addons: bool = False) -> CachedCatalog:
                return cached

            service.catalog_of = catalog_of  # type: ignore[method-assign]

            async def resolve(connection: Any, wanted: dict[str, Any], **kw: Any) -> Track:
                n = wanted["title"].split()[-1]
                return Track(
                    id=f"native-{n}", artist="B", title=wanted["title"], release=f"demo:al{n}"
                )

            service.resolver.resolve = AsyncMock(side_effect=resolve)
            native = Titled()
            service.client.call = native.call
            service.client.playlist = native.read

            async def track(connection: Any, ident: str, **kw: Any) -> Track:
                entry = Titled.song(ident)
                return Track(id=ident, artist=entry["artist"], title=entry["title"])

            service.native_track = AsyncMock(side_effect=track)
            batches: list[int] = []

            async def prepare(
                connection: Any, chosen: list[Track], *a: Any, **kw: Any
            ) -> list[str]:
                batches.append(len(chosen))
                for chosen_song in chosen:  # as check_tracks reads them: through the cache
                    await cached.album(CatalogRef.parse(chosen_song.release).id)
                return [s.id for s in chosen]

            service.check_tracks = AsyncMock(side_effect=prepare)
            lines = "\n".join(f"B — Song {n}" for n in range(12))
            ident = await service.imports.create("owner", "slow-releases", "Slow", lines)
            run = (await service.repo.runs(ident))[0]["id"]
            review = await process_until(service, run, "preview")
            key = service.imports.review_key(review["data"])
            await service.imports.action("owner", ident, run, "publish", "0", key)
            asked.clear()
            cached.album.cache_clear()  # type: ignore[attr-defined]  # (answers expired)
            service.settings.run_seconds = 0.3  # (a batch: half of it, about 4 releases)
            await service.process(run)
            first = await service.repo.run(run)
            assert first["state"] == "publishing"
            assert 0 < len(first["data"]["native_ids"]) < 12  # saved before the time ran out
            await process_until(service, run, "published")
            assert len(batches) > 1 and sum(batches) == 12
            assert sorted(asked) == sorted(f"al{n}" for n in range(12))  # each checked once
            assert native.playlist is not None
            assert [e["title"] for e in native.playlist["entry"]] == [
                f"Song {n}" for n in range(12)
            ]

    anyio.run(run)
