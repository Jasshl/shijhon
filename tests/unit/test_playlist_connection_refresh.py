"""Salted credential refreshes preserve running work; personal choices still fence it."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from shijhon.playlists.model import Definition, PlaylistError
from tests.unit.test_playlist_requests import service_at

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("changed", ["credentials", "identity", "ai", "history"])
async def test_generation_changes_only_for_playlist_settings(tmp_path: Path, changed: str):
    async with service_at(tmp_path / "db") as service:
        repo = service.repo
        before = await repo.connection("owner")
        generation = (await repo.store.fetchone("SELECT generation FROM playlist_owners"))[0]
        values = {
            "credentials": {"token": SecretStr("fresh-token"), "salt": SecretStr("fresh-salt")},
            "identity": {"username": "Renamed"},
            "ai": {"model": "another-model"},
            "history": {"history_key": SecretStr("another-key")},
        }[changed]
        await repo.connect(before.model_copy(update=values))
        after = await repo.connection("owner")
        current = (await repo.store.fetchone("SELECT generation FROM playlist_owners"))[0]
        assert (current == generation) == (changed == "credentials")
        assert before.same_settings(after) == (changed == "credentials")
        assert after == before.model_copy(update=values)


@pytest.mark.parametrize("change_settings", [False, True])
async def test_refresh_during_generation_preserves_work_and_publication(
    tmp_path: Path, change_settings: bool
):
    async with service_at(tmp_path / "db") as service:
        original = await service.connection("owner")
        mix = await service.repo.create("owner", Definition())
        run = await service.repo.enqueue(mix)

        async def generate(connection, row):
            values = {"token": SecretStr("fresh-token"), "salt": SecretStr("fresh-salt")}
            if change_settings:
                values["model"] = "changed-model"
            await service.repo.connect(connection.model_copy(update=values))
            return {"selected": []}

        async def publish(connection, row, ident, data):
            # Work keeps the still-valid old native pair; stored credentials are fresh.
            assert connection == original
            assert (await service.repo.connection("owner")).token != original.token
            await service.repo.update(ident, "published", data)

        service.generate = AsyncMock(side_effect=generate)
        service.publish = AsyncMock(side_effect=publish)
        await service.process(run)
        saved = await service.repo.run(run)
        if change_settings:
            assert saved["state"] == "failed" and "connection changed" in saved["error"].lower()
            service.publish.assert_not_called()
        else:
            assert saved["state"] == "published"
            service.publish.assert_awaited_once()
            await service.ai_connection(original)


async def test_refresh_during_history_import_keeps_the_cursor_and_listens(tmp_path: Path):
    async with service_at(tmp_path / "db", ai=False) as service:
        original = (await service.connection("owner")).model_copy(
            update={"history_user": "invented-history", "history_key": SecretStr("invented-key")}
        )
        await service.repo.connect(original)

        async def ask(connection, method, **params):
            await service.repo.connect(
                connection.model_copy(
                    update={"token": SecretStr("fresh"), "salt": SecretStr("salt2")}
                )
            )
            return {
                "recenttracks": {
                    "@attr": {"totalPages": "1", "total": "1", "perPage": "200", "page": "1"},
                    "track": [
                        {
                            "artist": {"#text": "Cloud Orchard"},
                            "name": "A Quiet Room",
                            "album": {"#text": "Morning"},
                            "date": {"uts": str(params["to"] - 100)},
                        }
                    ],
                }
            }

        service.history.ask = AsyncMock(side_effect=ask)
        listens, _ = await service.history.sync(original)
        assert len(listens) == 1 and listens[0]["title"] == "A Quiet Room"
        row = await service.repo.store.fetchone("SELECT history_until FROM playlist_owners")
        assert row[0] > 0
        # A real AI/history choice change must still fail even though credentials do not.
        await service.repo.connect(
            original.model_copy(update={"history_key": SecretStr("changed")})
        )
        with pytest.raises(PlaylistError):
            await service.ai_connection(original)
