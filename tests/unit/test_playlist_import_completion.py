"""Completion keeps published identities and native edits, using invented music only."""

from __future__ import annotations

import copy
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from starlette.responses import HTMLResponse

from shijhon.dashboard.playlist_imports import Pages
from shijhon.playlists import import_completion
from shijhon.playlists.import_completion import align, changes, merge
from shijhon.playlists.imports import Imports
from shijhon.playlists.model import PlaylistError, Track
from tests.unit.test_playlist_import_batches import Native, prepare_native, process_until
from tests.unit.test_playlist_requests import service_at

SONGS = {
    "native-one": ("A", "One"),
    "native-two": ("B", "Two"),
    "native-three": ("C", "Three"),
    "native-four": ("D", "Four"),
    "native-five": ("E", "Five"),
    "native-owner": ("X", "Owner song"),
}


def info(ident: str) -> tuple[str, str]:
    return SONGS.get(ident, ("B", "Two"))


class CompletingNative(Native):
    def __init__(self) -> None:
        super().__init__()
        self.removals: list[list[int]] = []
        self.lost_remove = False
        self.snapshots: list[list[str]] = []
        self.replace_fault = ""

    def song(self, ident: str) -> dict[str, str]:  # type: ignore[override]
        artist, title = info(ident)
        return {"id": ident, "artist": artist, "title": title}

    def edit(self, ids: list[str] | None = None, **fields: Any) -> None:
        """An owner's edit in Navidrome: entries and/or details, with a new change time."""
        assert self.playlist
        if ids is not None:
            self.playlist["entry"] = [self.song(i) for i in ids]
            self.playlist["songCount"] = len(ids)
        self.playlist.update(fields)
        self.changed += 1
        self.playlist["changed"] = str(self.changed)

    def ids(self) -> list[str]:
        assert self.playlist
        return [s["id"] for s in self.playlist["entry"]]

    async def call(self, connection: Any, method: str, params: Any = ()) -> dict[str, Any]:
        params = list(params)
        fields = dict(params)
        if method == "createPlaylist" and "playlistId" in fields:
            assert self.playlist and fields["playlistId"] == self.playlist["id"]
            assert "name" not in fields
            ids = [v for k, v in params if k == "songId"]
            self.mutations.append(("replace", ids))
            fault, self.replace_fault = self.replace_fault, ""
            if fault == "refused":
                raise PlaylistError("Synthetic refusal.")
            if fault == "changed":
                self.edit(comment="Edited while sending")
                raise PlaylistError("Synthetic lost reply.")
            if fault == "songs":
                self.edit([*self.ids(), "native-one"])
                raise PlaylistError("Synthetic lost reply.")
            self.changed += 1
            self.playlist.update(
                changed=str(self.changed), entry=[self.song(i) for i in ids], songCount=len(ids)
            )
            self.snapshots.append(ids)
            if fault == "lost":
                raise PlaylistError("Synthetic lost reply.")
            return {"playlist": copy.deepcopy(self.playlist)}
        indexes = [int(v) for k, v in params if k == "songIndexToRemove"]
        result = await super().call(connection, method, params)
        assert self.playlist
        if indexes:
            self.removals.append(indexes)
            for index in sorted(indexes, reverse=True):
                self.playlist["entry"].pop(index)
            self.playlist["songCount"] = len(self.playlist["entry"])
        self.snapshots.append(self.ids())
        if indexes and self.lost_remove:
            self.lost_remove = False
            raise PlaylistError("Synthetic lost removal reply.")
        return result


def find(service: Any, *titles: str) -> None:
    """Make these invented titles findable in the library from now on."""

    async def resolve(connection: Any, wanted: dict[str, Any], **kw: Any) -> Track | None:
        if wanted["title"] not in titles:
            return None
        ident = next(i for i, (_, title) in SONGS.items() if title == wanted["title"])
        return Track(id=ident, artist=info(ident)[0], title=wanted["title"])

    service.resolver.resolve = AsyncMock(side_effect=resolve)


async def partial(service: Any, text: str, *found: str) -> tuple[str, str, CompletingNative]:
    prepare_native(service)
    native = CompletingNative()
    service.client.call = native.call
    service.client.playlist = native.read

    async def track(connection: Any, ident: str, **kw: Any) -> Track:
        return Track(id=ident, artist=info(ident)[0], title=info(ident)[1])

    service.native_track = AsyncMock(side_effect=track)
    find(service, *(found or ("One",)))
    ident = await service.imports.create("owner", "completion-key", "Ordered", text)
    run = (await service.repo.runs(ident))[0]["id"]
    preview = await process_until(service, run, "preview")
    await service.imports.action(
        "owner",
        ident,
        run,
        "publish",
        str(sum(r["state"] != "matched" for r in preview["data"]["rows"])),
        service.imports.review_key(preview["data"]),
    )
    await process_until(service, run, "published")
    native.snapshots.clear()
    native.mutations.clear()
    return ident, run, native


async def recheck(service: Any, ident: str) -> tuple[str, dict[str, Any]]:
    previous = (await service.repo.runs(ident))[0]
    await service.imports.action(
        "owner", ident, previous["id"], "recheck", "", service.imports.review_key(previous["data"])
    )
    run = (await service.repo.runs(ident))[0]["id"]
    assert run != previous["id"]
    return run, await process_until(service, run, "preview")


async def approve(service: Any, ident: str, run: str, preview: dict[str, Any]) -> None:
    await service.imports.action(
        "owner",
        ident,
        run,
        "publish",
        str(len(import_completion.missing_entries(preview["data"]["rows"]))),
        service.imports.review_key(preview["data"]),
    )


# The merge itself: entry numbers are 1-based import rows; ids are native song IDs.


def matched(ident: str) -> dict[str, Any]:
    return {"state": "matched", "track": {"id": ident, "artist": "", "title": ident}}


def unresolved() -> dict[str, Any]:
    return {"state": "unresolved", "track": None}


def planned(
    published: list[str],
    published_rows: list[int | None],
    current: list[str],
    rows: list[dict[str, Any]],
    missing: list[int],
) -> tuple[list[str], list[dict[str, Any]]]:
    current_rows = align(published, published_rows, current)
    snapshot = [
        {"id": i, "row": n} if n is not None else {"id": i, "artist": "", "title": i}
        for i, n in zip(current, current_rows, strict=True)
    ]
    plan = merge(snapshot, rows, missing)
    rows_by_number = {n: r["track"]["id"] for n, r in enumerate(rows, 1) if r["track"]}
    target = [rows_by_number[s["row"]] if s.get("add") else s["id"] for s in plan]
    # Every entry that was there stays, in the same order.
    assert [s["id"] for s in plan if "id" in s] == current
    return target, plan


def test_owner_example_keeps_edits_and_places_found_songs_next_to_their_neighbours() -> None:
    # Import A B C D E; C and E missing, so A B D was published. The owner removed B
    # and added X; Recheck finds C and E.
    rows = [matched("a"), matched("b"), matched("c"), matched("d"), matched("e")]
    target, plan = planned(["a", "b", "d"], [1, 2, 4], ["a", "d", "x"], rows, [3, 5])
    assert target == ["a", "c", "d", "e", "x"]
    words = changes({"plan": plan}, rows)
    assert words == [
        {"kind": "add", "song": "c", "after": "a"},
        {"kind": "add", "song": "e", "after": "d"},
    ]


def test_merge_follows_moved_entries_and_inserts_at_the_start() -> None:
    rows = [matched("a"), matched("b"), matched("c"), matched("d"), matched("e")]
    # Moved: D to the front keeps entry 4, so E follows it.
    target, _ = planned(["a", "b", "d"], [1, 2, 4], ["d", "a", "b"], rows, [3, 5])
    assert target == ["d", "e", "a", "b", "c"]
    # No earlier entry: before the nearest later one, after the owner's own song.
    target, plan = planned(["b", "c"], [2, 3], ["x", "b", "c"], rows[:3], [1])
    assert target == ["x", "a", "b", "c"]
    assert changes({"plan": plan}, rows)[0]["after"] == "x"
    target, plan = planned(["b", "c"], [2, 3], ["b", "c"], rows[:3], [1])
    assert target == ["a", "b", "c"]
    assert changes({"plan": plan}, rows) == [{"kind": "add", "song": "a", "after": ""}]


def test_merge_when_the_owner_removed_the_anchor_or_everything() -> None:
    rows = [matched("a"), matched("b"), matched("c"), matched("d")]
    target, _ = planned(["a", "b", "d"], [1, 2, 4], ["a", "d"], rows, [3])
    assert target == ["a", "c", "d"]
    target, _ = planned(["a", "b", "d"], [1, 2, 4], ["d"], rows, [3])
    assert target == ["c", "d"]
    target, _ = planned(["a", "b", "d"], [1, 2, 4], ["x"], rows, [3])
    assert target == ["x", "c"]
    target, _ = planned(["a", "b"], [1, 2], [], rows[:3], [3])
    assert target == ["c"]


def test_merge_uses_a_song_the_owner_already_added_and_keeps_repeats_separate() -> None:
    rows = [matched("a"), matched("b"), matched("c"), matched("d")]
    target, plan = planned(["a", "b", "d"], [1, 2, 4], ["a", "b", "d", "c"], rows, [3])
    assert target == ["a", "b", "d", "c"]
    assert [s for s in plan if s.get("found")] == [{"id": "c", "row": 3, "found": True}]
    assert changes({"plan": plan}, rows) == [{"kind": "found", "song": "c", "after": ""}]
    # A song the owner added once covers one repeated entry; the other is added.
    rows = [matched("a"), matched("c"), matched("a"), matched("c")]
    target, _ = planned(["a", "a"], [1, 3], ["a", "a", "c"], rows, [2, 4])
    assert target == ["a", "a", "c", "c"]
    # Repeated rows stay separate entries in their own positions.
    target, _ = planned(["a", "a"], [1, 3], ["a", "a"], rows, [2, 4])
    assert target == ["a", "c", "a", "c"]
    # Catalog recordings have no native ID yet and are never treated as present.
    rows = [matched("a"), matched("sh.so.demo.c")]
    target, _ = planned(["a"], [1], ["a", "x"], rows, [2])
    assert target == ["a", "sh.so.demo.c", "x"]


def test_merge_skips_entries_still_missing_and_alignment_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [matched("a"), unresolved(), matched("c")]
    target, _ = planned(["a"], [1], ["a"], rows, [2, 3])
    assert target == ["a", "c"]
    # Heavily repeated IDs skip the quadratic matcher and pair occurrences in order.
    monkeypatch.setattr(import_completion, "ALIGN_WORK", 1)
    assert align(["a", "b", "a", "c"], [1, 2, 3, 4], ["c", "x", "a", "a", "b"]) == [
        4,
        None,
        1,
        3,
        2,
    ]


def test_alignment_of_a_large_reordered_playlist_stays_fast() -> None:
    """Swapped neighbours in a long list of distinct songs: the quadratic matcher is not
    used for so large an edited middle; every song keeps its entry."""
    previous = [f"song-{n}" for n in range(8000)]
    current = previous[:]
    for n in range(0, len(current) - 1, 2):
        current[n], current[n + 1] = current[n + 1], current[n]
    rows: list[int | None] = list(range(1, len(previous) + 1))
    started = time.perf_counter()
    aligned = align(previous, rows, current)
    assert time.perf_counter() - started < 0.5
    assert aligned == [int(song.split("-")[1]) + 1 for song in current]


def test_alignment_keeps_the_right_copy_of_a_repeated_song_in_a_long_list() -> None:
    """The owner removed the first of two copies of a song in a long playlist and added
    a song at the end: the first copy's entry is the removed one, not the second's."""
    songs = [f"s{n}" for n in range(1, 601)]
    previous = ["x", *songs[:300], "x", *songs[300:]]
    rows: list[int | None] = list(range(1, len(previous) + 1))
    current = [*songs[:300], "x", *songs[300:], "added"]
    aligned = align(previous, rows, current)
    assert aligned[300] == 302 and 1 not in aligned  # entry 1 was removed
    assert aligned[-1] is None and aligned[:300] == list(range(2, 302))


def test_alignment_with_many_copies_of_one_song_stays_fast() -> None:
    previous = [song for n in range(500) for song in ("x", f"s{n}")]
    rows: list[int | None] = list(range(1, len(previous) + 1))
    current = list(reversed(previous))
    started = time.perf_counter()
    aligned = align(previous, rows, current)
    assert time.perf_counter() - started < 0.2
    assert sorted(n for n in aligned if n is not None) == sorted(rows)  # nothing lost


# Completion through the service with a synthetic Navidrome.


def test_one_write_keeps_owner_edits_details_and_records_entries_for_the_next_recheck(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            text = "A — One\nB — Two\nC — Three\nD — Four\nE — Five"
            ident, first, native = await partial(service, text, "One", "Two", "Four")
            assert native.ids() == ["native-one", "native-two", "native-four"]
            native.edit(
                ["native-one", "native-four", "native-owner"],
                comment="Mine",
                name="Renamed",
                public=True,
            )
            details = {k: native.playlist[k] for k in ("id", "name", "comment", "public")}  # type: ignore[index]
            find(service, "Three", "Five")
            next_run, preview = await recheck(service, ident)
            assert native.mutations == []
            states = [r["state"] for r in preview["data"]["rows"]]
            assert states == ["matched", "removed", "matched", "matched", "matched"]
            assert service.resolver.resolve.call_count == 2
            words = changes(preview["data"]["completion"], preview["data"]["rows"])
            assert words == [
                {"kind": "add", "song": "C · Three", "after": "A · One"},
                {"kind": "add", "song": "E · Five", "after": "D · Four"},
            ]
            await approve(service, ident, next_run, preview)
            done = await process_until(service, next_run, "published")
            target = ["native-one", "native-three", "native-four", "native-five", "native-owner"]
            assert native.ids() == target
            assert native.mutations == [("replace", target)]
            assert {k: native.playlist[k] for k in details} == details  # type: ignore[index]
            assert done["data"]["playlist_ids"] == target
            assert done["data"]["entry_rows"] == [1, 3, 4, 5, None]
            mix = await service.repo.mix(ident, "owner")
            assert mix["playlist_id"] == native.playlist["id"]  # type: ignore[index]
            assert (await service.repo.run(first))["state"] == "published"
            # Nothing is missing now; a removed entry is not offered again.
            with pytest.raises(PlaylistError, match="no missing entries"):
                await recheck(service, ident)

    anyio.run(run)


def test_only_imported_entries_are_recorded_as_publication_additions(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, _, native = await partial(service, "A — One\nB — Two")
            native.edit(["native-one", "native-owner"])
            recorded: list[tuple[list[str], list[str]]] = []

            async def published(principal: Any, selected: Any, ids: Any, db: Any) -> None:
                recorded.append(([s.id for s in selected], list(ids)))

            service.published = published
            find(service, "Two")
            next_run, preview = await recheck(service, ident)
            await approve(service, ident, next_run, preview)
            await process_until(service, next_run, "published")
            assert native.ids() == ["native-one", "native-two", "native-owner"]
            assert recorded == [(["native-one", "native-two"], ["native-one", "native-two"])]

    anyio.run(run)


@pytest.mark.parametrize("fault", ["lost", "refused", "changed", "songs"])
def test_one_write_reconciles_an_uncertain_answer(tmp_path: Path, fault: str) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, _, native = await partial(service, "A — One\nB — Two")
            find(service, "Two")
            next_run, preview = await recheck(service, ident)
            await approve(service, ident, next_run, preview)
            native.replace_fault = fault
            if fault == "lost":
                # Applied, answer lost: the readback confirms it; nothing is sent again.
                await process_until(service, next_run, "published")
                assert len(native.mutations) == 1
                assert native.ids() == ["native-one", "native-two"]
                return
            result = await process_until(service, next_run, "uncertain")
            before = copy.deepcopy(native.playlist)
            if fault == "refused":
                assert "unchanged" in result["error"]
                assert native.ids() == ["native-one"]
                service.imports = Imports(service)
                await service.imports.action("owner", ident, next_run, "retry", "")
                await process_until(service, next_run, "published")
                # Still the reviewed playlist, so the same replacement was sent again.
                assert [m for m, _ in native.mutations] == ["replace", "replace"]
                assert native.ids() == ["native-one", "native-two"]
                return
            if fault == "changed":
                # Only the comment changed meanwhile: the owner's, kept; the songs are
                # still as reviewed, so Retry sends the same replacement again.
                assert "unchanged" in result["error"]
                await service.imports.action("owner", ident, next_run, "retry", "")
                await process_until(service, next_run, "published")
                assert native.ids() == ["native-one", "native-two"]
                assert native.playlist and native.playlist["comment"] == "Edited while sending"
                return
            assert "differs from this review" in result["error"]
            await service.imports.action("owner", ident, next_run, "retry", "")
            result = await process_until(service, next_run, "uncertain")
            assert "differs from this review" in result["error"]
            assert len(native.mutations) == 1 and native.playlist == before
            await service.imports.action("owner", ident, next_run, "stop", "")
            run2, _ = await recheck(service, ident)
            assert run2 != next_run

    anyio.run(run)


@pytest.mark.parametrize("stage", ["review", "approved", "preparation"])
def test_playlist_changed_after_review_is_never_written(tmp_path: Path, stage: str) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, original_run, native = await partial(service, "B — Two\nA — One")
            find(service, "Two", "One")
            next_run, preview = await recheck(service, ident)
            if stage == "review":
                native.edit(comment="Native edit")
                with pytest.raises(PlaylistError, match="changed since this review"):
                    await approve(service, ident, next_run, preview)
                # Recheck replaces the stale review and keeps the edit.
                fresh, review = await recheck(service, ident)
                assert (await service.repo.run(next_run))["state"] == "discarded"
                await approve(service, ident, fresh, review)
                await process_until(service, fresh, "published")
                assert native.playlist and native.playlist["comment"] == "Native edit"
                assert native.ids() == ["native-two", "native-one"]
                return
            await approve(service, ident, next_run, preview)
            if stage == "preparation":
                await service.process(next_run)
            native.edit(comment="Native edit")
            result = await process_until(service, next_run, "failed")
            assert "changed since this review" in result["error"]
            assert native.mutations == []
            assert native.playlist and native.playlist["comment"] == "Native edit"
            assert (await service.repo.run(original_run))["state"] == "published"
            assert "Recheck" in await page(service, ident)

    anyio.run(run)


async def page(service: Any, ident: str, **kw: Any) -> str:
    """The rendered context, as text, of the import page."""
    shown = await context(service, ident, **kw)
    return "Recheck" if shown["recheck_available"] else ""


async def context(service: Any, ident: str, **kw: Any) -> dict[str, Any]:
    render = AsyncMock(return_value=HTMLResponse("page"))
    dashboard = SimpleNamespace(services=SimpleNamespace(playlists=service), _render=render)
    await Pages(dashboard).render(SimpleNamespace(user_id="owner"), ident=ident, **kw)  # type: ignore[arg-type]
    shown: dict[str, Any] = render.call_args.kwargs["shown"]
    return shown


def test_recheck_progress_counts_only_rechecked_entries_and_missing_view(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            text = "A — One\nB — Two\nA — One\nC — Three\nA — One"
            ident, _, native = await partial(service, text)
            shown = await context(service, ident, only_missing=True)
            assert [r["number"] for r in shown["rows"]] == [2, 4]
            assert all(r["note"] for r in shown["rows"])
            assert shown["missing"] == 2 and shown["recheck_available"]
            previous = (await service.repo.runs(ident))[0]
            await service.imports.action(
                "owner",
                ident,
                previous["id"],
                "recheck",
                "",
                service.imports.review_key(previous["data"]),
            )
            shown = await context(service, ident)
            assert (shown["checked"], shown["checking"]) == (0, 2)
            assert not shown["recheck_available"]
            run_id = shown["run"]["id"]
            await process_until(service, run_id, "preview")
            shown = await context(service, ident)
            assert (shown["checked"], shown["checking"]) == (2, 2)
            assert shown["recheck_available"] and shown["length"] == (3, 3)
            assert native.mutations == []

    anyio.run(run)


@pytest.mark.parametrize("method", ["one write", "staged"])
@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_completion_updates_same_playlist_in_source_order_with_repeated_missing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: str, method: str
) -> None:
    if method == "staged":
        monkeypatch.setattr(import_completion, "ONE_STEP_LIMIT", 100)

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            originals = ["A — One"] * 402
            at = {"first": 0, "middle": 201, "last": len(originals)}[position]
            originals[at:at] = ["B — Two"] * 2
            ident, original_run, native = await partial(service, "\n".join(originals))
            before = copy.deepcopy(native.playlist)
            saved = await service.repo.run(original_run)
            # Simulate a formerly published catalog result: its native ID remains usable
            # even when the old catalog has been replaced or removed.
            for row in saved["data"]["rows"]:
                if row["state"] == "matched":
                    row["track"].update(id="sh.so.demo.old", release="demo:old")
            await service.repo.update(original_run, "published", saved["data"])
            find(service, "Two")
            next_run, preview = await recheck(service, ident)
            assert native.playlist == before
            assert service.resolver.resolve.call_count == 1
            assert preview["data"]["completion"]["source_run"] == original_run
            assert preview["data"]["completion"]["staged"] is (method == "staged")
            assert all(not t["release"] for t in preview["data"]["selected"])
            assert (await service.repo.run(original_run))["data"] == saved["data"]
            await approve(service, ident, next_run, preview)
            completed = await process_until(service, next_run, "published")
            assert native.playlist and native.playlist["id"] == before["id"]
            expected = ["native-two" if s.startswith("B") else "native-one" for s in originals]
            assert native.ids() == expected
            assert completed["data"]["playlist_ids"] == expected
            if method == "one write":
                assert native.mutations == [("replace", expected)]
                return
            assert all(len(ids) <= 200 for _, ids in native.mutations)
            assert all(len(indexes) <= 200 for indexes in native.removals)
            assert all(Counter(ids)["native-one"] >= 402 for ids in native.snapshots)

    anyio.run(run)


@pytest.mark.parametrize("lost", ["append", "remove"])
def test_staged_completion_reconciles_lost_mutation_reply_after_restarting_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost: str
) -> None:
    monkeypatch.setattr(import_completion, "ONE_STEP_LIMIT", 2)

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, _, native = await partial(service, "B — Two\nA — One\nA — One")
            find(service, "Two")
            next_run, preview = await recheck(service, ident)
            await approve(service, ident, next_run, preview)
            native.lost_append = lost == "append"
            native.lost_remove = lost == "remove"
            failed = await process_until(service, next_run, "uncertain")
            mutation_count = len(native.mutations)
            service.imports = Imports(service)
            await service.imports.action("owner", ident, next_run, "retry", "")
            await service.process(next_run)
            assert len(native.mutations) == mutation_count
            assert (await service.repo.run(next_run))["data"]["rows"] == failed["data"]["rows"]
            await process_until(service, next_run, "published")
            assert native.ids() == ["native-two", "native-one", "native-one"]
            assert service.resolver.resolve.call_count == 1

    anyio.run(run)


@pytest.mark.parametrize("edit", ["songs", "details"])
@pytest.mark.parametrize("stage", ["append", "remove"])
def test_native_edits_stop_a_staged_completion_between_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, edit: str
) -> None:
    """Edited songs stop a large update; a new name, comment or visibility is the
    owner's, stays, and the update goes on."""
    monkeypatch.setattr(import_completion, "ONE_STEP_LIMIT", 100)

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, original_run, native = await partial(service, "B — Two\n" + "A — One\n" * 402)
            find(service, "Two")
            next_run, preview = await recheck(service, ident)
            await approve(service, ident, next_run, preview)
            for _ in range(20):
                await service.process(next_run)
                progress = (await service.repo.run(next_run))["data"]["import_publish"]
                if progress.get("appended", 0) > progress.get("prefix", 0) and (
                    stage == "append" or progress["removed"]
                ):
                    break
            else:
                raise AssertionError("did not reach the selected completion boundary")
            if edit == "details":
                native.edit(comment="Native edit", name="Renamed by the owner", public=True)
                await process_until(service, next_run, "published")
                assert native.ids() == ["native-two"] + ["native-one"] * 402
                assert native.playlist and native.playlist["comment"] == "Native edit"
                assert native.playlist["name"] == "Renamed by the owner"
                assert native.playlist["public"] is True
                return
            native.edit([*native.ids(), "native-two"])
            mutations = len(native.mutations)
            result = await process_until(service, next_run, "uncertain")
            assert "edits are kept" in result["error"]
            assert len(native.mutations) == mutations
            assert native.ids()[-1] == "native-two"
            assert (await service.repo.run(original_run))["state"] == "published"

    anyio.run(run)


def test_missing_rows_survive_no_match_discard_and_explicit_later_recheck(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, _, native = await partial(service, "A — One\nB — Two\nB — Two")
            before = copy.deepcopy(native.playlist)
            next_run, preview = await recheck(service, ident)
            assert [r["state"] for r in preview["data"]["rows"]] == [
                "matched",
                "unresolved",
                "unresolved",
            ]
            assert preview["data"]["completion"]["plan"] == [{"id": "native-one", "row": 1}]
            await service.imports.action("owner", ident, next_run, "discard", "")
            find(service, "Two")
            final_run, final_review = await recheck(service, ident)
            assert native.playlist == before
            assert service.resolver.resolve.call_count == 1
            await approve(service, ident, final_run, final_review)
            await process_until(service, final_run, "published")
            assert native.ids() == ["native-one", "native-two", "native-two"]

    anyio.run(run)


@pytest.mark.parametrize("stage", ["append", "remove"])
def test_stop_keeps_all_existing_recordings_and_only_explicit_resume_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    monkeypatch.setattr(import_completion, "ONE_STEP_LIMIT", 100)

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, _, native = await partial(service, "B — Two\n" + "A — One\n" * 402)
            find(service, "Two")
            next_run, preview = await recheck(service, ident)
            await approve(service, ident, next_run, preview)
            for _ in range(20):
                await service.process(next_run)
                data = (await service.repo.run(next_run))["data"]
                progress = data["import_publish"]
                if stage == "append" and progress.get("appended", 0) > progress.get("prefix", 0):
                    break
                if stage == "remove" and progress.get("removed", 0) > 0:
                    break
            else:
                raise AssertionError("did not reach the selected completion boundary")
            await service.imports.action("owner", ident, next_run, "stop", "")
            stopped = await service.repo.run(next_run)
            assert stopped["state"] == "discarded"
            assert native.playlist
            assert Counter(native.ids())["native-one"] >= 402
            unchanged = copy.deepcopy(native.playlist)
            for _ in range(3):
                await service.process(next_run)
            assert native.playlist == unchanged
            key = service.imports.review_key(stopped["data"])
            # Its temporary copies would otherwise look like the owner's own songs.
            with pytest.raises(PlaylistError, match="extra copies"):
                await service.imports.action("owner", ident, next_run, "recheck", "", key)
            with pytest.raises(PlaylistError):
                await service.imports.action("other", ident, next_run, "resume", "", key)
            with pytest.raises(PlaylistError, match="review changed"):
                await service.imports.action("owner", ident, next_run, "resume", "", "stale")
            await service.imports.action("owner", ident, next_run, "resume", "", key)
            await process_until(service, next_run, "published")
            assert native.ids() == ["native-two"] + ["native-one"] * 402

    anyio.run(run)


@pytest.mark.parametrize("state", ["failed", "uncertain"])
def test_recheck_refuses_extra_copies_of_a_failed_staged_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    monkeypatch.setattr(import_completion, "ONE_STEP_LIMIT", 100)

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, _, native = await partial(service, "B — Two\n" + "A — One\n" * 402)
            find(service, "Two")
            next_run, preview = await recheck(service, ident)
            await approve(service, ident, next_run, preview)
            for _ in range(20):
                await service.process(next_run)
                progress = (await service.repo.run(next_run))["data"]["import_publish"]
                if progress.get("appended", 0) > progress.get("prefix", 0):
                    break
            else:
                raise AssertionError("did not reach a staged boundary")
            # A failure between batches leaves the temporary copies in place.
            await service.repo.store.execute(
                "UPDATE playlist_runs SET state=? WHERE id=?", [state, next_run]
            )
            assert Counter(native.ids())["native-one"] > 402
            failed = await service.repo.run(next_run)
            key = service.imports.review_key(failed["data"])
            if state == "failed":
                with pytest.raises(PlaylistError, match="extra copies"):
                    await service.imports.action("owner", ident, next_run, "recheck", "", key)
            with pytest.raises(PlaylistError, match="extra copies"):
                await service.imports.recheck(await service.repo.mix(ident, "owner"))

    anyio.run(run)


def test_owner_edit_during_an_unfinished_large_update_needs_an_explicit_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner adds a song while a large update has its temporary copies in place, then
    the update stops. The copies cannot be told from the owner's songs any more: Recheck
    waits for Resume, Retry or the owner's explicit "Keep playlist as it is"."""
    monkeypatch.setattr(import_completion, "ONE_STEP_LIMIT", 100)

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, _, native = await partial(service, "B — Two\n" + "A — One\n" * 402)
            find(service, "Two")
            next_run, preview = await recheck(service, ident)
            await approve(service, ident, next_run, preview)
            for _ in range(20):
                await service.process(next_run)
                progress = (await service.repo.run(next_run))["data"]["import_publish"]
                if progress.get("appended", 0) > progress.get("prefix", 0):
                    break
            else:
                raise AssertionError("did not reach a staged boundary")
            native.edit([*native.ids(), "native-two"])  # the owner's own addition
            await service.imports.action("owner", ident, next_run, "stop", "")
            stopped = await service.repo.run(next_run)
            key = service.imports.review_key(stopped["data"])
            with pytest.raises(PlaylistError, match="extra copies"):
                await service.imports.action("owner", ident, next_run, "recheck", "", key)
            with pytest.raises(PlaylistError, match="changed"):
                await service.imports.action("owner", ident, next_run, "keep", "", "stale")
            await service.imports.action("owner", ident, next_run, "keep", "", key)
            kept = await service.repo.run(next_run)
            assert kept["state"] == "discarded" and kept["data"]["kept_as_is"]
            with pytest.raises(PlaylistError, match="no longer available"):
                key = service.imports.review_key(kept["data"])
                await service.imports.action("owner", ident, next_run, "resume", "", key)
            unchanged = native.ids()
            _, review = await recheck(service, ident)
            # What is in the playlist now is the owner's: nothing is removed or re-added.
            assert [s["id"] for s in review["data"]["completion"]["plan"]] == unchanged

    anyio.run(run)


def test_one_write_has_no_resume_and_stopped_update_is_rechecked_without_copies(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, _, native = await partial(service, "A — One\nB — Two")
            find(service, "Two")
            next_run, preview = await recheck(service, ident)
            await approve(service, ident, next_run, preview)
            # Stopped after its write but before it was recorded as published.
            original = service.native_track.side_effect
            service.native_track.side_effect = PlaylistError("Synthetic verification failure.")
            await process_until(service, next_run, "uncertain")
            assert native.ids() == ["native-one", "native-two"]
            await service.imports.action("owner", ident, next_run, "stop", "")
            stopped = await service.repo.run(next_run)
            key = service.imports.review_key(stopped["data"])
            with pytest.raises(PlaylistError, match="no longer available"):
                await service.imports.action("owner", ident, next_run, "resume", "", key)
            service.native_track.side_effect = original
            fresh, review = await recheck(service, ident)
            words = changes(review["data"]["completion"], review["data"]["rows"])
            assert words == [{"kind": "found", "song": "B · Two", "after": ""}]
            await approve(service, ident, fresh, review)
            await process_until(service, fresh, "published")
            assert native.ids() == ["native-one", "native-two"]
            assert [m for m, _ in native.mutations] == ["replace"]

    anyio.run(run)


def test_recheck_refuses_a_deleted_or_foreign_playlist(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, _, _ = await partial(service, "A — One\nB — Two")

            async def gone(connection: Any, ident: str) -> dict[str, Any]:
                raise PlaylistError("Navidrome could not complete the request.")

            service.client.playlist = gone
            with pytest.raises(PlaylistError, match="If you deleted it"):
                await recheck(service, ident)
            assert len(await service.repo.runs(ident)) == 1

    anyio.run(run)


def test_completion_cannot_change_published_recordings_or_cross_account_library(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, original_run, _ = await partial(service, "A — One\nB — Two")
            original = await service.repo.run(original_run)
            key = service.imports.review_key(original["data"])
            with pytest.raises(PlaylistError):
                await service.imports.action("other", ident, original_run, "recheck", "", key)
            service.client.native.library_id = 2
            with pytest.raises(PlaylistError):
                await service.imports.action("owner", ident, original_run, "recheck", "", key)
            service.client.native.library_id = 1
            next_run, preview = await recheck(service, ident)
            with pytest.raises(PlaylistError, match="stay fixed"):
                await service.imports.action(
                    "owner",
                    ident,
                    next_run,
                    "choose",
                    "",
                    service.imports.review_key(preview["data"]),
                    entry="1",
                    choice="native-two",
                )
            assert (await service.repo.run(next_run))["data"] == preview["data"]
            with pytest.raises(PlaylistError, match="no longer current"):
                await service.imports.action("owner", ident, original_run, "recheck", "", key)
            # A pinned recording changed behind the review is refused at publication.
            tampered = copy.deepcopy(preview["data"])
            tampered["rows"][0]["track"]["id"] = "native-two"
            tampered["selected"][0]["id"] = "native-two"
            tampered["import_review"] = {"total": 2, "excluded": [2]}
            mix = await service.repo.mix(ident, "owner")
            with pytest.raises(PlaylistError, match="stay fixed"):
                await service.imports.reviewed(mix, tampered)

    anyio.run(run)


def test_unconfirmed_staged_append_is_not_repeated_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(import_completion, "ONE_STEP_LIMIT", 1)

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, _, native = await partial(service, "B — Two\nA — One")
            find(service, "Two")
            next_run, preview = await recheck(service, ident)
            await approve(service, ident, next_run, preview)
            native.before_append = True
            await process_until(service, next_run, "uncertain")
            before = copy.deepcopy(native.playlist)
            mutations = len(native.mutations)
            native.before_append = False
            await service.imports.action("owner", ident, next_run, "retry", "")
            result = await process_until(service, next_run, "uncertain")
            assert "No mutation was repeated" in result["error"]
            assert len(native.mutations) == mutations
            assert native.playlist == before

    anyio.run(run)
