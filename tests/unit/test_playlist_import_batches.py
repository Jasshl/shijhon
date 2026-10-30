"""Saved matching/native append batches; every identity and byte here is synthetic."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

from shijhon.collection.contracts import Actor
from shijhon.playlists.import_publish import APPEND_BATCH, PREPARE_BATCH
from shijhon.playlists.imports import BATCH_ROWS, Imports
from shijhon.playlists.model import PlaylistError, Track
from tests.unit.test_playlist_requests import connected, service_at


async def process_until(service: Any, run: str, state: str) -> dict[str, Any]:
    for _ in range(100):
        result = await service.repo.run(run)
        if result["state"] == state:
            return result
        assert result["state"] in ("queued", "publishing"), result
        await service.process(run)
    raise AssertionError("bounded synthetic run did not finish")


class Native:
    def __init__(self) -> None:
        self.playlist: dict[str, Any] | None = None
        self.mutations: list[tuple[str, list[str]]] = []
        self.lost_append = False
        self.before_append = False
        self.changed = 0

    @staticmethod
    def song(ident: str) -> dict[str, str]:
        return {
            "id": ident,
            "artist": "A" if ident == "native-one" else "B",
            "title": "One" if ident == "native-one" else "Two",
        }

    async def call(self, connection: Any, method: str, params: Any = ()) -> dict[str, Any]:
        params = list(params)
        fields = dict(params)
        if method == "getPlaylists":
            return {
                "playlists": {"playlist": [copy.deepcopy(self.playlist)] if self.playlist else []}
            }
        if method == "createPlaylist":
            self.changed += 1
            ids = [v for k, v in params if k == "songId"]
            self.mutations.append((method, ids))
            self.playlist = {
                "id": "new-native",
                "owner": "owner",
                "name": fields["name"],
                "changed": str(self.changed),
                "public": False,
                "comment": "",
                "entry": [self.song(i) for i in ids],
                "songCount": len(ids),
            }
            return {"playlist": copy.deepcopy(self.playlist)}
        assert method == "updatePlaylist"
        assert self.playlist
        ids = [v for k, v in params if k == "songIdToAdd"]
        self.mutations.append((method, ids))
        if ids and self.before_append:
            raise PlaylistError("Synthetic transport failed before append confirmation.")
        self.changed += 1
        self.playlist["changed"] = str(self.changed)
        self.playlist["entry"].extend(self.song(i) for i in ids)
        self.playlist["songCount"] = len(self.playlist["entry"])
        if "name" in fields:
            self.playlist["name"] = fields["name"]
        if ids and self.lost_append:
            self.lost_append = False
            raise PlaylistError("Synthetic transport lost the successful append response.")
        return {}

    async def read(self, connection: Any, ident: str) -> dict[str, Any]:
        assert self.playlist and ident == self.playlist["id"]
        return copy.deepcopy(self.playlist)


async def importing(service: Any, count: int) -> tuple[str, str]:
    async def resolve(connection: Any, wanted: dict[str, Any], **kw: Any) -> Track:
        return Track(
            id="native-one" if wanted["title"] == "One" else "native-two",
            artist=wanted["artist"],
            title=wanted["title"],
        )

    service.resolver.resolve = AsyncMock(side_effect=resolve)
    ident = await service.imports.create(
        "owner", "batch-key", "Ordered batch", "A — One\nB — Two\n" * (count // 2)
    )
    job = (await service.repo.runs(ident))[0]
    return ident, job["id"]


def prepare_native(service: Any) -> Native:
    native = Native()
    service.client.call = native.call
    service.client.playlist = native.read

    async def track(connection: Any, ident: str, **kw: Any) -> Track:
        return Track(
            id=ident,
            artist="A" if ident == "native-one" else "B",
            title="One" if ident == "native-one" else "Two",
        )

    service.native_track = AsyncMock(side_effect=track)

    async def prepare(connection: Any, selected: list[Track], *args: Any, **kw: Any) -> list[str]:
        return [s.id for s in selected]

    service.check_tracks = AsyncMock(side_effect=prepare)
    return native


def test_large_matching_batches_resume_after_recreating_import_service_and_keep_repeats(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, run_id = await importing(service, 1202)
            await service.process(run_id)
            first = await service.repo.run(run_id)
            assert first["state"] == "queued" and len(first["data"]["rows"]) == BATCH_ROWS
            assert service.resolver.resolve.call_count == 2
            service.imports = Imports(service)  # No task-local matching cache is required.
            result = await process_until(service, run_id, "preview")
            assert len(result["data"]["selected"]) == 1202
            assert [s["id"] for s in result["data"]["selected"]] == [
                "native-one",
                "native-two",
            ] * 601
            assert service.resolver.resolve.call_count == 2
            await service.imports.action(
                "owner", ident, run_id, "publish", "0", service.imports.review_key(result["data"])
            )
            native = prepare_native(service)
            published = await process_until(service, run_id, "published")
            assert native.playlist
            assert [s["id"] for s in native.playlist["entry"]] == published["data"]["native_ids"]
            assert len(native.playlist["entry"]) == 1202
            assert all(len(ids) <= APPEND_BATCH for _, ids in native.mutations)
            assert all(
                len(call.args[1]) <= PREPARE_BATCH for call in service.check_tracks.call_args_list
            )

    anyio.run(run)


def test_distinct_recordings_checkpoint_per_batch_with_bounded_json_volume(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            count = 2000

            async def resolve(connection: Any, wanted: dict[str, Any], **kw: Any) -> Track:
                return Track(
                    id=f"native-{wanted['title']}", artist=wanted["artist"], title=wanted["title"]
                )

            service.resolver.resolve = AsyncMock(side_effect=resolve)
            ident = await service.imports.create(
                "owner",
                "distinct-key",
                "Distinct recordings",
                "\n".join(f"A — Song{i}" for i in range(count)),
            )
            run_id = (await service.repo.runs(ident))[0]["id"]
            checkpoint = service.imports.checkpoint
            sizes, cursors = [], []

            async def counted(
                ident: str, state: str, data: dict[str, Any], *, start: int | None = None
            ) -> bool:
                delta = (
                    {**data, "rows": data["rows"][start : data["cursor"]]}
                    if start is not None
                    else data
                )
                sizes.append(len(json.dumps(delta).encode()))
                cursors.append(data["cursor"])
                return await checkpoint(ident, state, data, start=start)

            service.imports.checkpoint = counted
            result = await process_until(service, run_id, "preview")
            assert [s["id"] for s in result["data"]["selected"]] == [
                f"native-Song{i}" for i in range(count)
            ]
            assert service.resolver.resolve.call_count == count
            assert cursors == list(range(BATCH_ROWS, count + 1, BATCH_ROWS))
            # The R1 regression submitted 709.7 MB for these 2,000 distinct rows.
            # Include byte volume as well as update count to catch oversized payloads.
            assert sum(sizes) < 2 * 1024 * 1024

    anyio.run(run)


@pytest.mark.parametrize("final_name", [False, True])
def test_preupgrade_create_intent_reconciles_before_or_after_final_rename(
    tmp_path: Path, final_name: bool
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, run_id = await importing(service, 4)
            result = await process_until(service, run_id, "preview")
            await service.imports.action(
                "owner", ident, run_id, "publish", "0", service.imports.review_key(result["data"])
            )
            data = (await service.repo.run(run_id))["data"]
            data.update(
                native_ids=["native-one", "native-two"] * 2,
                intent="create",
                provisional="Shijhon preparing legacy-unique",
                created_id="new-native",
            )
            assert "import_publish" not in data
            native = prepare_native(service)
            native.playlist = {
                "id": "new-native",
                "owner": "owner",
                "changed": "1",
                "public": False,
                "comment": "",
                "songCount": 4,
                "name": data["definition"]["name"] if final_name else data["provisional"],
                "entry": [native.song(i) for i in data["native_ids"]],
            }
            await service.repo.update(run_id, "uncertain", data)
            await service.imports.action("owner", ident, run_id, "retry", "")
            service.imports = Imports(service)
            published = await process_until(service, run_id, "published")
            assert published["data"]["native_ids"] == ["native-one", "native-two"] * 2
            assert native.playlist["name"] == data["definition"]["name"]
            assert native.mutations == ([] if final_name else [("updatePlaylist", [])])
            assert service.check_tracks.call_count == 0  # Never reprepare or replace.

    anyio.run(run)


@pytest.mark.parametrize(
    "edit", ["id", "name", "owner", "public", "comment", "entries", "recording"]
)
def test_preupgrade_recovery_still_refuses_native_edits(tmp_path: Path, edit: str) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, run_id = await importing(service, 2)
            result = await process_until(service, run_id, "preview")
            await service.imports.action(
                "owner", ident, run_id, "publish", "0", service.imports.review_key(result["data"])
            )
            data = (await service.repo.run(run_id))["data"]
            data.update(
                native_ids=["native-one", "native-two"],
                intent="create",
                provisional="Shijhon preparing legacy-unique",
                created_id="new-native",
            )
            native = prepare_native(service)
            native.playlist = {
                "id": "new-native",
                "owner": "owner",
                "changed": "1",
                "public": False,
                "comment": "",
                "songCount": 2,
                "name": data["definition"]["name"],
                "entry": [native.song(i) for i in data["native_ids"]],
            }
            if edit == "entries":
                native.playlist["entry"].reverse()
            elif edit == "recording":
                service.native_track = AsyncMock(
                    return_value=Track(id="native-one", artist="Other", title="Changed")
                )
            else:
                native.playlist[edit] = True if edit == "public" else "Owner's edit"
                if edit == "id":
                    service.client.playlist = AsyncMock(return_value=native.playlist)
            await service.repo.update(run_id, "publishing", data)
            for _ in range(10):
                await service.process(run_id)
                result = await service.repo.run(run_id)
                if result["state"] == "uncertain":
                    break
            assert result["state"] == "uncertain", result
            if edit == "id":
                assert "identity could not be verified" in result["error"]
            assert native.mutations == []

    anyio.run(run)


@pytest.mark.parametrize("applied", [True, False])
def test_uncertain_append_is_reconciled_without_replaying_the_mutation(
    tmp_path: Path, applied: bool
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, run_id = await importing(service, 402)
            result = await process_until(service, run_id, "preview")
            await service.imports.action(
                "owner", ident, run_id, "publish", "0", service.imports.review_key(result["data"])
            )
            native = prepare_native(service)
            native.lost_append = applied
            native.before_append = not applied
            for _ in range(20):
                await service.process(run_id)
                result = await service.repo.run(run_id)
                if result["state"] == "uncertain":
                    break
            assert result["state"] == "uncertain" and native.playlist
            assert len(native.playlist["entry"]) == (400 if applied else 200)
            assert len(native.mutations) == 2
            service.imports = Imports(service)
            await service.imports.action("owner", ident, run_id, "retry", "")
            await service.process(run_id)
            result = await service.repo.run(run_id)
            assert len(native.mutations) == 2  # The pending append was never resent.
            if applied:
                assert result["state"] == "publishing"
                await process_until(service, run_id, "published")
                assert len(native.playlist["entry"]) == 402
            else:
                assert result["state"] == "uncertain"
                assert "No append was repeated" in result["error"]
                await service.imports.action("owner", ident, run_id, "stop", "")
                await service.process(run_id)
                assert (await service.repo.run(run_id))["state"] == "discarded"
                assert len(native.playlist["entry"]) == 200 and len(native.mutations) == 2

    anyio.run(run)


def test_native_edit_or_cancel_between_batches_never_continues_publication(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, run_id = await importing(service, 402)
            result = await process_until(service, run_id, "preview")
            await service.imports.action(
                "owner", ident, run_id, "publish", "0", service.imports.review_key(result["data"])
            )
            native = prepare_native(service)
            for _ in range(10):
                await service.process(run_id)
                if native.playlist:
                    break
            assert native.playlist and len(native.playlist["entry"]) == 200
            native.playlist["entry"].insert(0, {"id": "owners-new-song"})
            native.playlist["songCount"] += 1
            await service.process(run_id)
            assert (await service.repo.run(run_id))["state"] == "uncertain"
            assert len(native.mutations) == 1
            await service.imports.action("owner", ident, run_id, "stop", "")
            await service.process(run_id)
            assert len(native.mutations) == 1 and len(native.playlist["entry"]) == 201

    anyio.run(run)


def test_stop_during_matching_does_not_get_overwritten_by_a_late_checkpoint(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            source = "\n".join(f"A — Song{i}" for i in range(BATCH_ROWS + 10))
            ident = await service.imports.create(
                "owner", "matching-stop-key", "Stopped matching", source
            )
            run_id = (await service.repo.runs(ident))[0]["id"]
            entered, release = anyio.Event(), anyio.Event()
            calls = []

            async def blocked(connection: Any, wanted: dict[str, Any], **kw: Any) -> Track:
                calls.append(wanted["title"])
                if len(calls) == BATCH_ROWS + 5:
                    entered.set()
                    await release.wait()
                return Track(id=f"native-{wanted['title']}", artist="A", title=wanted["title"])

            service.resolver.resolve = blocked
            await service.process(run_id)
            before = await service.repo.run(run_id)
            assert before["state"] == "queued" and len(before["data"]["rows"]) == BATCH_ROWS
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(service.process, run_id)
                await entered.wait()
                async with service.changes:
                    await service.imports.action("owner", ident, run_id, "stop", "")
                release.set()
            stopped = await service.repo.run(run_id)
            assert stopped["state"] == "discarded" and stopped["data"] == {
                **before["data"],
                "stopped": True,
            }
            assert len(calls) == BATCH_ROWS + 5
            await service.process(run_id)
            assert (await service.repo.run(run_id))["state"] == "discarded"

    anyio.run(run)


def test_connection_change_during_matching_prevents_late_batch_save_or_more_work(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            _ident, run_id = await importing(service, 2)
            entered, release = anyio.Event(), anyio.Event()
            calls = []

            async def blocked(*args: Any, **kw: Any) -> Track:
                calls.append(1)
                entered.set()
                await release.wait()
                return Track(id="native-one", artist="A", title="One")

            service.resolver.resolve = blocked
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(service.process, run_id)
                await entered.wait()
                await service.repo.connect(
                    connected().model_copy(update={"history_source": "navidrome"})
                )
                release.set()
            result = await service.repo.run(run_id)
            assert result["state"] == "failed" and "connection changed" in result["error"]
            assert result["data"]["rows"] == [] and calls == [1]

    anyio.run(run)


def test_asx_remote_base_cannot_select_an_unrelated_native_basename(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            reader = SimpleNamespace(
                authorize=AsyncMock(return_value=Actor("owner", frozenset({"1"}))),
                songs=AsyncMock(return_value=[{"id": "native-one", "path": "Unrelated/One.flac"}]),
            )
            service.imports.library_reader = reader
            service.native_track = AsyncMock(
                return_value=Track(id="native-one", artist="Other", title="Unrelated")
            )
            service.resolver.resolve = AsyncMock()
            ident = await service.imports.create(
                "owner",
                "asx-base-key",
                "Remote ASX",
                '<ASX><BASE HREF="https://example.invalid/remote/"/>'
                '<ENTRY><REF HREF="One.flac"/></ENTRY></ASX>',
                format="asx",
            )
            run_id = (await service.repo.runs(ident))[0]["id"]
            result = await process_until(service, run_id, "preview")
            assert result["data"]["rows"][0]["state"] == "unresolved"
            assert result["data"]["selected"] == []
            reader.songs.assert_not_called()
            service.native_track.assert_not_called()
            service.resolver.resolve.assert_not_called()

    anyio.run(run)


def test_paths_use_owner_metadata_untagged_names_native_ids_and_explicit_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shijhon.playlists.imports.PATH_PAGE", 2)

    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            songs = [
                {
                    "id": "native-one",
                    "artist": "Unknown Artist",
                    "title": "01",
                    "path": "Organized/Room/01.flac",
                },
                {
                    "id": "native-two",
                    "artist": "Unknown Artist",
                    "title": "01",
                    "path": "Other/Room/01.flac",
                },
                {
                    "id": "native-three",
                    "artist": "Unknown Artist",
                    "title": "02",
                    "path": "Organized/Room/02.flac",
                },
            ]
            songs.sort(key=lambda s: s["id"])
            calls = []

            async def read(
                owner: str, library: str, offset: int, limit: int
            ) -> list[dict[str, Any]]:
                assert owner == "owner" and library == "1" and limit == 2
                calls.append(offset)
                return songs[offset : offset + limit]

            service.imports.library_reader = SimpleNamespace(
                authorize=AsyncMock(return_value=Actor("owner", frozenset({"1"}))), songs=read
            )

            async def native(connection: Any, ident: str, **kw: Any) -> Track:
                assert connection.user_id == "owner" and kw == {"library_bound": True}
                song = next(s for s in songs if s["id"] == ident)
                return Track(id=ident, artist=song["artist"], title=song["title"])

            service.native_track = AsyncMock(side_effect=native)
            service.resolver.resolve = AsyncMock()
            source = "#EXTM3U\nC:\\Music\\Organized\\Room\\01.flac\n01.flac\nfile:///Music/Organized/Room/02.flac\n"
            ident = await service.imports.create(
                "owner", "paths-key", "Untagged paths", source, filename="local.m3u"
            )
            run_id = (await service.repo.runs(ident))[0]["id"]
            await service.process(run_id)
            assert (await service.repo.run(run_id))["state"] == "queued"
            result = await process_until(service, run_id, "preview")
            assert calls == [0, 2]
            assert [r["state"] for r in result["data"]["rows"]] == [
                "matched",
                "ambiguous",
                "matched",
            ]
            assert result["data"]["rows"][0]["track"]["id"] == "native-one"
            choices = result["data"]["rows"][1]["choices"]
            assert {c["track"]["id"] for c in choices} == {"native-one", "native-two"}
            service.resolver.resolve.assert_not_called()
            with pytest.raises(PlaylistError, match="review changed"):
                await service.imports.action(
                    "owner", ident, run_id, "choose", "", "stale", entry="2", choice="native-two"
                )
            await service.imports.action(
                "owner",
                ident,
                run_id,
                "choose",
                "",
                service.imports.review_key(result["data"]),
                entry="2",
                choice="native-two",
            )
            result = await process_until(service, run_id, "preview")
            assert [s["id"] for s in result["data"]["selected"]] == [
                "native-one",
                "native-two",
                "native-three",
            ]
            assert result["data"]["rows"][1]["note"] == "Recording chosen explicitly"

    anyio.run(run)


def test_unknown_local_paths_do_not_silently_substitute_metadata_and_remote_urls_are_not_fetched(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            service.client.call = AsyncMock(return_value={"searchResult3": {"song": []}})
            service.imports.library_reader = SimpleNamespace(
                authorize=AsyncMock(return_value=Actor("owner", frozenset({"1"}))),
                songs=AsyncMock(return_value=[]),
            )
            service.resolver.resolve = AsyncMock(
                return_value=Track(id="native-one", artist="A", title="One")
            )
            ident = await service.imports.create(
                "owner",
                "location-key",
                "Safe locations",
                "artist,title,path\nA,One,/private/unknown.flac\nA,One,http://127.0.0.1/secret\n",
                csv_file=True,
            )
            run_id = (await service.repo.runs(ident))[0]["id"]
            result = await process_until(service, run_id, "preview")
            assert [r["state"] for r in result["data"]["rows"]] == ["unresolved", "matched"]
            assert service.resolver.resolve.call_count == 1
            assert all(call.args[1] == "search3" for call in service.client.call.call_args_list)

    anyio.run(run)


def test_stop_during_preparation_prevents_native_mutation_and_late_progress(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident, run_id = await importing(service, 2)
            result = await process_until(service, run_id, "preview")
            await service.imports.action(
                "owner", ident, run_id, "publish", "0", service.imports.review_key(result["data"])
            )
            native = prepare_native(service)
            entered, release = anyio.Event(), anyio.Event()

            async def blocked(*args: Any, **kwargs: Any) -> list[str]:
                entered.set()
                await release.wait()
                return ["native-one", "native-two"]

            service.check_tracks = blocked
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(service.process, run_id)
                await entered.wait()
                async with service.changes:
                    await service.imports.action("owner", ident, run_id, "stop", "")
                release.set()
            result = await service.repo.run(run_id)
            assert result["state"] == "discarded" and result["data"]["native_ids"] == []
            assert native.mutations == []
            await service.process(run_id)
            assert native.mutations == []

    anyio.run(run)


def test_path_matching_without_current_caller_native_access_is_recoverable(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident = await service.imports.create(
                "owner", "reconnect-key", "Local path", "Album/One.flac", format="m3u"
            )
            run_id = (await service.repo.runs(ident))[0]["id"]
            await service.process(run_id)
            result = await service.repo.run(run_id)
            assert result["state"] == "failed" and "Sign in again" in result["error"]
            reader = SimpleNamespace(
                authorize=AsyncMock(return_value=Actor("other", frozenset({"1"}))),
                songs=AsyncMock(return_value=[]),
            )
            service.imports.library_reader = reader
            await service.imports.action("owner", ident, run_id, "retry", "")
            await service.process(run_id)
            assert (await service.repo.run(run_id))["state"] == "failed"
            reader.songs.assert_not_called()
            reader.authorize.return_value = Actor("owner", frozenset({"1"}))
            await service.imports.action("owner", ident, run_id, "retry", "")
            result = await process_until(service, run_id, "preview")
            assert result["data"]["rows"][0]["state"] == "unresolved"

    anyio.run(run)
