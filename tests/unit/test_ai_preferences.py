"""Synthetic AI policy checks; model transports are mocks and never contact providers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest
from pydantic import ValidationError

from shijhon.access import Principal
from shijhon.playlists.model import AIModels, AIPreferences, Definition, PlaylistError, Track
from shijhon.playlists.prompt import Proposals, generate
from shijhon.playlists.requests import PlaylistRequest
from tests.unit.test_playlist_requests import connected, service_at


def test_personal_models_are_isolated_and_persist_without_host_credentials(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            admin = await service.connection("owner")
            await service.configure_shared_ai(admin, allowed_models=["approved", "approved"])
            host = await service.repo.shared_ai()
            assert host["allowed_models"] == ["approved"]
            for owner in ("a", "b"):
                await service.repo.connect(connected(owner, use_shared_ai=True))
            service.client.principal = AsyncMock(
                side_effect=lambda c: Principal(c.user_id, c.username, False, frozenset({1}))
            )
            await service.choose_ai_model("a", "approved")
            a, b = await service.connection("a"), await service.connection("b")
            assert (await service.ai_connection(a)).model == "approved"
            assert (await service.ai_connection(b)).model == "synthetic"
            assert await service.repo.shared_ai() == host
            assert await service.repo.ai_preferences("a") == AIPreferences(model="approved")
            rows = await service.repo.store.fetchall(
                "SELECT * FROM saved_settings WHERE section='playlist-ai-preferences'"
            )
            raw = json.dumps([dict(r) for r in rows])
            assert "approved" in raw and "https://" not in raw and "native-secret" not in raw
            with pytest.raises(PlaylistError, match="approved"):
                await service.choose_ai_model("a", "unapproved")
            assert (await service.repo.ai_preferences("a")).model == "approved"
            with pytest.raises(PlaylistError, match="Only administrators"):
                await service.configure_shared_ai(admin, allowed_models=["unapproved"])
            await service.choose_ai_model("a", "", use_shared_ai=False)
            assert not (await service.connection("a")).use_shared_ai
            assert await service.repo.ai_preferences("a") == AIPreferences()
            await service.choose_ai_model("a", "approved", use_shared_ai=True)
            assert (await service.ai_connection(await service.connection("a"))).model == "approved"
        async with service_at(tmp_path / "db", ai=False) as reopened:
            assert (await reopened.repo.ai_preferences("a")).model == "approved"
            assert (
                await reopened.ai_connection(await reopened.connection("a"))
            ).model == "approved"
            await reopened.repo.disconnect("a")
            assert await reopened.repo.ai_preferences("a") == AIPreferences()
            assert await reopened.repo.shared_ai() == host

    anyio.run(run)


@pytest.mark.parametrize("change", ["model", "disabled", "demoted", "credentials"])
def test_ai_execution_rechecks_revocation_and_current_role(tmp_path: Path, change: str) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            admin = await service.connection("owner")
            await service.configure_shared_ai(admin, allowed_models=["approved"])
            await service.repo.connect(connected("a", use_shared_ai=True))
            await service.choose_ai_model("a", "approved")
            original = await service.connection("a")
            if change == "model":
                await service.configure_shared_ai(admin, allowed_models=[])
            elif change == "disabled":
                await service.configure_shared_ai(connected(), allowed_models=[])
            elif change == "demoted":
                original = admin
                service.client.principal = AsyncMock(
                    return_value=Principal("owner", "owner", False, frozenset({1}))
                )
            else:
                service.client.validate = AsyncMock(
                    side_effect=PlaylistError("Expired credentials")
                )
            service.curator.api = AsyncMock(side_effect=AssertionError("no model request"))
            service.curator.cli = AsyncMock(side_effect=AssertionError("no runner"))
            with pytest.raises(PlaylistError):
                await service.curator.exchange(
                    original, "synthetic", schema=Proposals, instructions="music"
                )
            with pytest.raises(PlaylistError):
                await service.curator.select(
                    original,
                    Definition(count=1, ai=True),
                    [Track(id="native", artist="Glass Orchard", title="Lantern", unfamiliar=True)],
                )
            service.curator.api.assert_not_awaited()
            service.curator.cli.assert_not_awaited()

    anyio.run(run)


def test_model_revocation_between_proposal_and_order_stops_second_call(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            admin = await service.connection("owner")
            await service.configure_shared_ai(admin, allowed_models=["approved"])
            await service.repo.connect(connected("a", use_shared_ai=True))
            await service.choose_ai_model("a", "approved")

            async def ai(*args: object, **kwargs: object) -> object:
                await service.configure_shared_ai(admin, allowed_models=[])
                return {
                    "tracks": [
                        {"artist": "Glass Orchard", "title": "Lantern", "album": "", "version": ""}
                    ]
                }

            service.curator.api = AsyncMock(side_effect=ai)
            service.resolver.resolve = AsyncMock(
                return_value=Track(id="native", artist="Glass Orchard", title="Lantern")
            )
            with pytest.raises(PlaylistError, match="no longer approved"):
                await generate(
                    service,
                    await service.connection("a"),
                    Definition(
                        mode="prompt", brief="Quiet music", ai=True, count=1, cadence="manual"
                    ),
                )
            assert service.curator.api.await_count == 1

    anyio.run(run)


def test_model_allowlist_is_bounded_and_uses_runner_model_validation(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        AIModels(allowed_models=["m"] * 51)
    for value in ("", "a\nb", " leading", "x" * 101):
        with pytest.raises(ValidationError):
            AIModels(allowed_models=[value])

    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            admin = await service.connection("owner")
            runner = admin.model_copy(update={"backend": "codex", "model": "", "endpoint": ""})
            with pytest.raises(ValidationError):
                await service.configure_shared_ai(runner, allowed_models=["--flag"])
            assert not await service.repo.shared_ai()
            await service.configure_shared_ai(runner, allowed_models=["safe-model"])
            assert (await service.ai_choices("owner"))["allowed_models"] == ["safe-model"]

    anyio.run(run)


def test_queued_refresh_stops_before_model_and_pauses_after_revocation(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            admin = await service.connection("owner")
            await service.configure_shared_ai(admin, allowed_models=["approved"])
            await service.repo.connect(connected("a", use_shared_ai=True))
            await service.choose_ai_model("a", "approved")
            mix, _ = await service.requests.create(
                "a",
                "queued-refresh",
                PlaylistRequest(prompt="Quiet music", save_instructions=True, cadence="weekly"),
            )
            await service.repo.store.execute(
                "UPDATE playlist_mixes SET enabled=1 WHERE id=?", [mix]
            )
            ident = (await service.repo.runs(mix))[0]["id"]
            await service.repo.store.execute(
                "UPDATE playlist_runs SET automatic=1 WHERE id=?", [ident]
            )
            owner = await service.repo.store.fetchone(
                "SELECT generation FROM playlist_owners WHERE id='a'"
            )
            # The dashboard preference form must not renew authorization or invalidate
            # saved recurring instructions when only the personal model changes.
            await service.choose_ai_model("a", "synthetic", use_shared_ai=True)
            assert (
                await service.repo.store.fetchone(
                    "SELECT generation FROM playlist_owners WHERE id='a'"
                )
                == owner
            )
            await service.choose_ai_model("a", "approved", use_shared_ai=True)
            await service.configure_shared_ai(admin, allowed_models=[])
            service.curator.api = AsyncMock(side_effect=AssertionError("no model request"))
            service.publish = AsyncMock(side_effect=AssertionError("no native publication"))
            await service.process(ident)
            saved = await service.repo.run(ident)
            assert saved["state"] == "failed" and "no longer approved" in saved["error"]
            assert not (await service.repo.mix(mix, "a"))["enabled"]
            service.curator.api.assert_not_awaited()
            service.publish.assert_not_awaited()

    anyio.run(run)
