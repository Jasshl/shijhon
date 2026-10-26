"""Negative provenance, shared freshness gates and serialized owner actions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from shijhon.catalog.base import SearchResults
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.fill.fills import ChoiceRefused, Decision, Fills
from shijhon.fill.library_pass import RETRY_SECONDS, LibraryPass
from shijhon.locks import KeyedLocks
from shijhon.matching.matcher import (
    MATCH_REVISION,
    Match,
    Matcher,
    Outcome,
    OwnedAlbum,
    OwnedSong,
    confirm,
    owned_fingerprint,
)
from shijhon.navidrome.client import NavidromeError
from shijhon.store import Store

OWNED = OwnedAlbum(
    "native",
    "Night Letters",
    "Invented Harbor",
    2020,
    (OwnedSong("one", "First", "Invented Harbor", 1, 2, 3000),),
)


def test_negative_fingerprint_includes_every_matcher_input() -> None:
    original = owned_fingerprint(OWNED)
    for name, value in (
        ("title", "Retitled"),
        ("artist", "Other"),
        ("year", 2021),
        ("clean", True),
    ):
        assert owned_fingerprint(replace(OWNED, **{name: value})) != original
    values = {
        "id": "other",
        "title": "Other",
        "artist": "Other",
        "disc": 2,
        "number": 3,
        "duration_ms": 4000,
        "isrcs": ("ZZSHJ0000001",),
        "track_total": 5,
        "disc_total": 1,
        "disc_tagged": True,
    }
    assert set(values) == {f.name for f in fields(OwnedSong)}
    for name, value in values.items():
        changed = replace(OWNED.songs[0], **{name: value})
        assert owned_fingerprint(replace(OWNED, songs=(changed,))) != original, name
    two = replace(OWNED, songs=(*OWNED.songs, replace(OWNED.songs[0], id="two")))
    assert owned_fingerprint(two) == owned_fingerprint(replace(two, songs=two.songs[::-1]))


class Rig:
    def __init__(self, store: Store) -> None:
        self.owned = OWNED
        self.loads = 0
        self.searches = 0
        self.now = 1000000.0
        self.outage = False

        async def search(*args: Any, **kwargs: Any) -> SearchResults:
            self.searches += 1
            return SearchResults()

        async def native_album(ident: str) -> dict[str, Any]:
            return {"name": self.owned.title, "artist": self.owned.artist}

        async def subsonic(*args: Any) -> dict[str, Any]:
            return {"searchResult3": {}}

        self.fills = Fills(
            store,
            SimpleNamespace(album=native_album, subsonic=subsonic),
            SimpleNamespace(),
            Matcher(SimpleNamespace(search=search)),
            scope="demo.xx",
            clock=lambda: self.now,
            negative_seconds=100,
        )

        async def owned(ident: str) -> OwnedAlbum:
            self.loads += 1
            if self.outage:
                raise NavidromeError("unavailable", code=0)
            return self.owned

        self.fills._owned = owned  # type: ignore[method-assign]


@pytest.mark.parametrize("planned", [False, True])
@pytest.mark.parametrize("change", ["legacy", "revision", "metadata", "expiry"])
def test_negative_refresh_then_cache_across_view_and_pass(
    tmp_path: Path, planned: bool, change: str
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        try:
            rig = Rig(store)
            fills = rig.fills
            await fills._record("native", Decision("none", "no match"), rig.owned, planned=planned)
            original_plan = (await fills._row("native"))["plan"]
            assert not await fills.due("native", dry_run=True)
            if change == "legacy":
                await store.execute(
                    "UPDATE album_matches SET match_revision=0,owned_fingerprint=NULL"
                )
            elif change == "revision":
                await store.execute(
                    "UPDATE album_matches SET match_revision=?", [MATCH_REVISION - 1]
                )
            elif change == "metadata":
                rig.owned = replace(rig.owned, year=2021)
            else:
                rig.now += 100
            # Both the pass (including dry run) and authenticated views see the same gate.
            assert await fills.due("native", dry_run=True)
            decision, _ = await fills.view("native", ("owner", "client"))
            assert decision is None
            assert rig.searches == 2  # a new real match, not the old dry-run none plan
            row = await fills._row("native")
            assert row["outcome"] == "none" and row["match_revision"] == MATCH_REVISION
            assert row["owned_fingerprint"] == owned_fingerprint(rig.owned)
            assert not await fills.due("native", dry_run=planned)
            rig.loads = 0
            assert await fills.viewable("native")
            assert rig.loads == 0  # before authentication: DB only
            assert await fills.view("native", ("owner", "client")) == (None, None)
            assert rig.loads == 1  # one metadata snapshot for a stable negative view
            assert rig.searches == 2
            if planned and change != "metadata":
                assert original_plan is not None  # legacy plan shape remains readable
        finally:
            await store.close()

    anyio.run(run)


def test_native_outage_keeps_negative_provenance(tmp_path: Path) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        try:
            rig = Rig(store)
            await rig.fills._record("native", Decision("none", "no match"), rig.owned)
            before = dict(await rig.fills._row("native"))
            rig.outage = True
            assert await rig.fills.view("native", ("owner", "client")) == (None, None)
            with pytest.raises(NavidromeError):
                await rig.fills.due("native")
            assert dict(await rig.fills._row("native")) == before
            assert rig.searches == 0
        finally:
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize("outcome", ["kept", "review", "complete", "filled", "deferred", "failed"])
def test_recheck_preserves_nonnegative_and_recovery_rows(tmp_path: Path, outcome: str) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        try:
            rig = Rig(store)
            await rig.fills._record("native", Decision(outcome, "owner decision"), rig.owned)
            await store.execute("UPDATE album_matches SET cleaned_at=123")
            before = dict(await rig.fills._row("native"))
            assert await rig.fills.recheck("native") is None
            assert dict(await rig.fills._row("native")) == before
            assert rig.searches == 0
        finally:
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize("action", ["recheck", "rematch"])
def test_keep_wins_while_recheck_or_rematch_waits(tmp_path: Path, action: str) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        try:
            rig = Rig(store)
            fills = rig.fills
            await fills._record("native", Decision("none", "no match"), rig.owned)
            entered, proceed = anyio.Event(), anyio.Event()
            original = fills._group_key

            async def delayed(ident: str) -> str:
                entered.set()
                await proceed.wait()
                return await original(ident)

            fills._group_key = delayed  # type: ignore[method-assign]

            async def request() -> None:
                try:
                    await getattr(fills, action)("native")
                except ChoiceRefused:
                    assert action == "rematch"

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(request)
                await entered.wait()
                fills._group_key = original  # type: ignore[method-assign]
                await fills.keep("native")
                proceed.set()
            assert (await fills._row("native"))["outcome"] == "kept"
            assert rig.searches == 0
        finally:
            await store.close()

    anyio.run(run)


def test_failed_refresh_keeps_plan_and_provenance_for_normal_retry(tmp_path: Path) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        try:
            rig = Rig(store)
            await rig.fills._record(
                "native",
                Decision("deferred", "existing plan", release_ref="demo:release"),
                rig.owned,
            )
            await store.execute("UPDATE album_matches SET cleaned_at=123")
            before = dict(await rig.fills._row("native"))
            await rig.fills._record(
                "native", Decision("failed", "temporary failure"), replace(rig.owned, year=2022)
            )
            failed = await rig.fills._row("native")
            for key in ("plan", "release_ref", "match_revision", "owned_fingerprint", "cleaned_at"):
                assert failed[key] == before[key]
            assert not rig.fills._retry_due(failed)
            rig.now += rig.fills.retry_seconds + 1
            assert rig.fills._retry_due(failed)
        finally:
            await store.close()

    anyio.run(run)


def test_rematch_waits_for_release_recovery_and_preserves_its_updated_record(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        try:
            rig = Rig(store)
            fills = rig.fills
            await fills._record(
                "native", Decision("deferred", "old plan", release_ref="demo:release"), rig.owned
            )
            locks, waiting = KeyedLocks(), anyio.Event()

            @asynccontextmanager
            async def lock_for(ref: str) -> AsyncIterator[None]:
                waiting.set()
                async with locks.hold(ref):
                    yield

            fills.engine.lock_for = lock_for  # type: ignore[method-assign]

            async def request() -> None:
                with pytest.raises(ChoiceRefused, match="changed"):
                    await fills.rematch("native")

            async with anyio.create_task_group() as tasks, locks.hold("demo:release"):
                tasks.start_soon(request)
                await waiting.wait()
                await store.execute("UPDATE album_matches SET reason='recovered plan'")
            assert (await fills._row("native"))["reason"] == "recovered plan"
        finally:
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize("outcome", ["review", "complete", "deferred", "failed"])
def test_negative_refresh_preserves_another_group_members_settled_decision(
    tmp_path: Path, outcome: str
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        try:
            rig = Rig(store)
            fills = rig.fills
            other = replace(rig.owned, id="other")
            await fills._record("native", Decision("none", "old miss"), rig.owned)
            await fills._record("other", Decision(outcome, "settled decision"), other)
            await store.execute("UPDATE album_matches SET match_revision=0")
            protected = dict(await fills._row("other"))

            async def group(owned: OwnedAlbum) -> list[OwnedAlbum]:
                return [other]

            fills._group = group  # type: ignore[method-assign]
            decision = await fills.recheck("native")
            assert decision and decision.outcome == "review"
            assert dict(await fills._row("other")) == protected
            assert rig.searches == 0
        finally:
            await store.close()

    anyio.run(run)


def test_negative_pass_stops_on_first_native_outage_and_uses_short_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Scheduled(Exception):
        pass

    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        try:
            rig = Rig(store)
            ids = [f"negative{n}" for n in range(4)]
            for ident in ids:
                await rig.fills._record(ident, Decision("none", "fresh miss"), rig.owned)
            before = [dict(r) for r in await store.fetchall("SELECT * FROM album_matches")]

            async def albums() -> list[tuple[str, int, str]]:
                return [(ident, 1, "") for ident in ids]

            rig.fills.navidrome.album_counts = albums  # type: ignore[method-assign]
            library = LibraryPass(
                rig.fills,
                rig.fills.navidrome,
                rig.fills.matcher,
                mode="on",
                start_seconds=0,
            )
            rig.outage = True
            assert await library.run_once() is None
            assert rig.loads == 1 and rig.searches == 0
            assert [dict(r) for r in await store.fetchall("SELECT * FROM album_matches")] == before
            delays = []

            async def sleep(seconds: float) -> None:
                delays.append(seconds)
                if len(delays) == 2:
                    raise Scheduled

            monkeypatch.setattr("shijhon.fill.library_pass.anyio.sleep", sleep)
            rig.loads = 0
            with pytest.raises(Scheduled):
                await library.run()
            assert delays == [0, RETRY_SECONDS]
            assert rig.loads == 1
            assert [dict(r) for r in await store.fetchall("SELECT * FROM album_matches")] == before
        finally:
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize("action", ["keep", "recovery"])
def test_cross_credit_group_keeps_are_serialized_through_matching_and_recording(
    tmp_path: Path,
    action: str,
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        try:
            rig = Rig(store)
            fills = rig.fills
            source = replace(
                OWNED,
                title="Night Letters (Remastered)",
                artist="Invented Harbor feat. Guest",
                songs=(*OWNED.songs, replace(OWNED.songs[0], id="four", title="Fourth", number=4)),
            )
            target = replace(OWNED, id="target")
            albums = {source.id: source, target.id: target}
            ref = CatalogRef("demo", "letters")
            release = CatalogRelease(
                ref,
                OWNED.title,
                OWNED.artist,
                ReleaseKind.ALBUM,
                "2020",
                tracks=tuple(
                    CatalogTrack(
                        CatalogRef("demo", f"song{n}"),
                        "First" if n == 2 else "Fourth" if n == 4 else f"Song {n}",
                        OWNED.artist,
                        3000,
                        number=n,
                        album=ref,
                    )
                    for n in range(1, 6)
                ),
            )

            async def owned(ident: str) -> OwnedAlbum:
                return albums[ident]

            async def native(ident: str) -> dict[str, Any]:
                album = albums[ident]
                return {"name": album.title, "artist": album.artist}

            async def group(album: OwnedAlbum) -> list[OwnedAlbum]:
                return [m for m in albums.values() if m.id != album.id]

            entered, proceed, kept = anyio.Event(), anyio.Event(), anyio.Event()

            async def match(album: OwnedAlbum, *, pick: bool = False) -> Match:
                assert album == source
                entered.set()
                await proceed.wait()
                links = confirm(album, release)
                assert links
                return Match(Outcome.FILL, "", release, links)

            fills._owned = owned  # type: ignore[method-assign]
            fills._group = group  # type: ignore[method-assign]
            fills.navidrome.album = native  # type: ignore[method-assign]
            fills.matcher.match = match  # type: ignore[method-assign]
            await fills._record(source.id, Decision("none", "old miss"), source)
            await fills._record(target.id, Decision("none", "old miss"), target)
            assert await fills._group_key(source.id) == await fills._group_key(target.id)

            async def recheck() -> None:
                if action == "recovery":
                    with pytest.raises(ChoiceRefused, match="group changed"):
                        await fills.recheck(source.id)
                else:
                    decision = await fills.recheck(source.id)
                    assert decision and decision.outcome == "deferred"

            async def keep() -> None:
                await fills.keep(target.id)
                kept.set()

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(recheck)
                await entered.wait()
                assert fills._locks.held(target.id)  # every affected member stays locked
                if action == "keep":
                    tasks.start_soon(keep)
                    await anyio.wait_all_tasks_blocked()
                    assert not kept.is_set()
                else:
                    # Recovery has its own release lock. Revalidate changes that do not
                    # pass through owner-action group/album locks before any fill/record.
                    await store.execute(
                        "UPDATE album_matches SET outcome='kept' WHERE album_id=?", [target.id]
                    )
                proceed.set()
            assert (await fills._row(target.id))["outcome"] == "kept"
        finally:
            await store.close()

    anyio.run(run)
