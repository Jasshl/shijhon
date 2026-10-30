"""Batch scheduling and checkpoint regressions use synthetic import data only."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

from shijhon.playlists.import_formats import Entry
from shijhon.playlists.imports import BATCH_ROWS
from shijhon.playlists.requests import PlaylistRequest
from tests.unit.test_playlist_requests import service_at


def test_due_mix_runs_between_import_batch_rounds(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            first, _ = await service.requests.create(
                "owner", "long-import", PlaylistRequest(prompt="quiet")
            )
            second, _ = await service.requests.create(
                "owner",
                "due-mix-key",
                PlaylistRequest(prompt="quiet", save_instructions=True, cadence="daily"),
            )
            second_run = (await service.repo.runs(second))[0]
            await service.repo.update(second_run["id"], "published", {})
            await service.repo.store.execute(
                "UPDATE playlist_mixes SET enabled=1,due=? WHERE id=?", [time.time() + 1000, second]
            )
            turns = []

            async def process(ident: str) -> None:
                job = await service.repo.run(ident)
                turns.append(job["mix"])
                if len(turns) == 1:
                    await service.repo.store.execute(
                        "UPDATE playlist_mixes SET due=0 WHERE id=?", [second]
                    )
                else:
                    await service.repo.update(ident, "preview", {})

            service.process = process
            await service.tick()
            assert turns == [first, first, second]

    anyio.run(run)


def test_import_batches_reuse_parsed_inputs_and_persist_only_row_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident = await service.imports.create(
                "owner",
                "parsed-inputs",
                "Invented import",
                "\n".join(f"Glass Orchard — Lantern {n}" for n in range(BATCH_ROWS + 2)),
            )
            job = (await service.repo.runs(ident))[0]
            main_thread = threading.get_ident()
            validate = Entry.model_validate
            parsed = []

            def checked(value: Any, *args: Any, **kw: Any) -> Entry:
                assert threading.get_ident() != main_thread
                parsed.append(value)
                return validate(value, *args, **kw)

            monkeypatch.setattr(Entry, "model_validate", checked)
            service.imports.recording = AsyncMock(
                side_effect=lambda c, e, d: {
                    "state": "unresolved",
                    "track": None,
                    "choices": [],
                    "note": "No match",
                }
            )
            await service.process(job["id"])
            first = await service.repo.run(job["id"])
            assert first["state"] == "queued" and len(first["data"]["rows"]) == BATCH_ROWS
            assert len(parsed) == BATCH_ROWS + 2
            saved_inputs = service.imports.inputs[ident]
            # The next checkpoint does not send the growing rows list through json.dumps.
            dumps = json.dumps

            def bounded(value: Any, *args: Any, **kw: Any) -> str:
                if threading.get_ident() == main_thread and isinstance(value, list):
                    assert len(value) <= BATCH_ROWS
                return dumps(value, *args, **kw)

            monkeypatch.setattr("shijhon.playlists.imports.json.dumps", bounded)
            await service.process(job["id"])
            result = await service.repo.run(job["id"])
            assert result["state"] == "preview" and len(result["data"]["rows"]) == BATCH_ROWS + 2
            assert len(parsed) == BATCH_ROWS + 2 and service.imports.inputs[ident] is saved_inputs
            # Retrying from the middle replaces only that range, retaining the suffix.
            data = result["data"]
            data["rows"][1]["note"] = "Changed"
            data["cursor"] = 2
            assert await service.imports.checkpoint(job["id"], "preview", data, start=1)
            assert (await service.repo.run(job["id"]))["data"]["rows"] == data["rows"]

    anyio.run(run)
