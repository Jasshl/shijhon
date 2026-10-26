"""Download controls preserve inheritance, saved values and concurrent form safety."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Match

from shijhon.app import ShijhonApp
from shijhon.config import load_settings
from shijhon.dashboard import sections
from shijhon.dashboard.addons import parse_manifest, transfer_limit
from shijhon.dashboard.auth import Session
from shijhon.dashboard.fields import Invalid
from shijhon.dashboard.saved import Saved, effective, locked_keys
from shijhon.delivery.bulk import effective_capacity
from shijhon.delivery.netpolicy import Reach
from shijhon.delivery.sources import SourceRegistry, StoredSource
from shijhon.store import Store


def _settings(tmp_path: Path, **overrides: Any) -> Any:
    return load_settings(
        None,
        state_dir=tmp_path / "state",
        navidrome={"url": "http://127.0.0.1:9", "library_path": tmp_path},
        **overrides,
    )


def _rows(settings: Any, saved: Any = None, locked: Any = None) -> dict[str, Any]:
    return {
        row["name"]: row
        for _, rows in sections.rows(
            "delivery", settings, saved or {}, addon_names=[], locked=locked or {}
        )
        for row in rows
    }


@pytest.mark.parametrize(
    "typed,expected", [("", None), ("  ", None), ("0", 0), ("3.0", 3), ("1000", 1000)]
)
def test_transfer_override_distinguishes_inherit_custom_and_unlimited(
    typed: str, expected: int | None
) -> None:
    assert transfer_limit(typed) == expected


@pytest.mark.parametrize("typed", ["-1", "1001", "1,5", "nan", "unlimited"])
def test_transfer_override_rejects_invalid_values(typed: str) -> None:
    with pytest.raises(Invalid, match="whole number from 0 to 1000"):
        transfer_limit(typed)


def test_main_controls_show_effective_defaults_and_keep_saved_overrides(tmp_path: Path) -> None:
    configured = _settings(tmp_path)
    rows = _rows(configured)
    assert rows["bulk_downloads"]["text"] == "4"
    assert rows["addon_downloads"]["text"] == "2"
    assert rows["addon_preparations"]["text"] == "1"
    assert rows["user_downloads"]["text"] == "4"
    assert rows["user_downloads_per_hour"]["text"] == "0"
    assert rows["bulk_downloads"]["default"] == "Default 4 songs"
    assert not rows["bulk_downloads"]["restart"]
    assert rows["user_downloads"]["restart"]
    groups = {
        field.key: group
        for group, fields in sections.grouped("delivery", sections.fields_of("delivery"))
        for field in fields
    }
    assert not groups["bulk_downloads"].more and not groups["addon_downloads"].more
    assert groups["addon_preparations"].more
    saved = {
        "delivery": {
            "user_downloads": Saved(2, 1, "owner"),
            "user_downloads_per_hour": Saved(120, 1, "owner"),
        }
    }
    applied, problems = effective(configured, saved, locked={})
    assert not problems
    assert applied.delivery.user_downloads == 2
    assert applied.delivery.user_downloads_per_hour == 120
    assert _rows(configured, saved)["user_downloads_per_hour"]["text"] == "120"


def test_download_routes_and_navigation_do_not_match_playlist_mix_routes(tmp_path: Path) -> None:
    from shijhon.dashboard.app import NAV

    dashboard = ShijhonApp(_settings(tmp_path)).dashboard
    assert ("downloads", "Downloads") in [item for _, items in NAV for item in items]
    routes = dashboard.app.routes[0].routes
    for path, method in (
        ("/downloads", "GET"),
        ("/downloads/connection", "POST"),
        ("/downloads/prepare", "POST"),
        ("/downloads/saved-selection/action", "POST"),
    ):
        matches = [
            route
            for route in routes
            if route.matches({"type": "http", "path": path, "method": method})[0] == Match.FULL
        ]
        assert len(matches) == 1 and matches[0].path.startswith("/downloads")


@pytest.mark.anyio
async def test_global_controls_respect_environment_locks_and_partial_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIJHON_DELIVERY__BULK_DOWNLOADS", "7")
    configured = _settings(tmp_path)
    locked = locked_keys(configured)
    saved = {"delivery": {"bulk_downloads": Saved(3, 1, "owner")}}
    row = _rows(configured, saved, locked)["bulk_downloads"]
    assert row["kind"] == "locked" and row["text"] == "7 songs" and row["hidden"]
    submitted = await sections.submit(
        "delivery",
        configured,
        saved,
        {"bulk_downloads": "999", "addon_downloads": "0"},
        addon_names=[],
        locked=locked,
    )
    assert not submitted.problems
    written = sections.writes("delivery", configured, saved, submitted, locked=locked)
    assert written.writes == {"addon_downloads": 0}


def test_addon_form_versions_include_transfer_overrides_and_shared_group(tmp_path: Path) -> None:
    dashboard = ShijhonApp(_settings(tmp_path)).dashboard
    addon = StoredSource(
        1, "First", "https://first.example.invalid", {}, True, 1, Reach.PUBLIC, None
    )
    original = dashboard._addon_version(addon)
    for changed in (
        replace(addon, downloads=0),
        replace(addon, preparations=3),
        replace(addon, capacity_group="shared-worker"),
    ):
        assert dashboard._addon_version(changed) != original
    _, more = dashboard._addon_rows(addon, None, {})
    rows = {row["name"]: row for row in more}
    assert rows["downloads"]["text"] == rows["preparations"]["text"] == ""
    assert "2" in rows["downloads"]["default"]
    assert "1" in rows["preparations"]["default"]
    assert rows["capacity_group"]["text"] == ""


def test_shared_effective_limits_show_inheritance_and_unlimited_overrides(tmp_path: Path) -> None:
    dashboard = ShijhonApp(_settings(tmp_path)).dashboard
    first = StoredSource(
        1,
        "First",
        "https://one.example.invalid/first",
        {},
        True,
        1,
        Reach.PUBLIC,
        None,
        downloads=5,
        preparations=0,
        capacity_group="worker",
    )
    alias = replace(
        first,
        id=2,
        name="Alias",
        base_url="https://one.example.invalid/alias",
        downloads=3,
        preparations=2,
        capacity_group="account",
    )
    other = replace(
        first,
        id=3,
        name="Other",
        base_url="https://two.example.invalid",
        downloads=0,
        preparations=None,
        capacity_group="account",
    )
    disabled = replace(first, id=4, enabled=False, downloads=1)
    independent = replace(
        first,
        id=5,
        base_url="https://three.example.invalid",
        downloads=None,
        preparations=None,
        capacity_group="",
    )
    capacities = effective_capacity([first, alias, other, disabled, independent], 2, 1)
    assert capacities == {1: (3, 1), 2: (3, 1), 3: (3, 1), 5: (2, 1)}
    _, more = dashboard._addon_rows(other, None, {}, capacity=capacities[other.id])
    rows = {row["name"]: row for row in more}
    assert rows["downloads"]["text"] == "0"
    assert rows["downloads"]["modified"]
    # Its own 0 (no limit) gives way to the strictest limit of the add-ons it shares with.
    assert rows["downloads"]["default"] == (
        "Default 2 songs, from Playback."
        " In effect: 3 songs, from the add-ons it shares limits with."
    )
    assert rows["preparations"]["text"] == ""
    assert rows["preparations"]["default"] == "Default 1 song, from Playback. In effect: 1 song."
    _, more = dashboard._addon_rows(independent, None, {}, capacity=capacities[independent.id])
    assert {row["name"]: row for row in more}["downloads"]["default"] == (
        "Default 2 songs, from Playback. In effect: 2 songs."
    )
    _, more = dashboard._addon_rows(disabled, None, {}, capacity=None)
    assert {row["name"]: row for row in more}["downloads"]["default"] == (
        "Default 2 songs, from Playback. Not in effect while off."
    )


@pytest.mark.anyio
async def test_addon_saves_preserve_omitted_fields_clear_inheritance_and_reject_stale_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = _settings(tmp_path)
    app = ShijhonApp(configured)
    store = await Store.open(tmp_path / "controls.sqlite3")
    sources = SourceRegistry(store)
    app.services = SimpleNamespace(store=store, sources=sources)
    dashboard = app.dashboard
    dashboard.attach(store, configured, {})
    session = Session("hash", "owner", "csrf", 9999999999, "user-id")
    refused: list[dict[str, Any]] = []

    async def refused_page(*args: Any, **kwargs: Any) -> PlainTextResponse:
        refused.append(kwargs)
        return PlainTextResponse("not saved", status_code=kwargs["status_code"])

    monkeypatch.setattr(dashboard, "addons_page", refused_page)
    try:
        source_id = await sources.add(
            "First",
            "https://first.example.invalid",
            downloads=3,
            preparations=2,
            capacity_group="shared-worker",
        )

        async def submit(values: dict[str, str], version: str | None = None) -> Any:
            addon = (await sources.stored())[0]
            dashboard.checks.learn(
                addon,
                parse_manifest({"id": "demo", "name": "First", "resources": ["stream", "isrc"]}),
            )
            request = Request({"type": "http", "path_params": {"addon": source_id}})
            request.state.form = {"version": version or dashboard._addon_version(addon), **values}
            return await dashboard.addon_settings(request, session)

        stale = dashboard._addon_version((await sources.stored())[0])
        assert (await submit({"downloads": "0"})).status_code == 303
        addon = (await sources.stored())[0]
        assert (addon.downloads, addon.preparations, addon.capacity_group) == (
            0,
            2,
            "shared-worker",
        )
        assert (await submit({"downloads": "1"}, stale)).status_code == 409
        assert (await sources.stored())[0].downloads == 0
        assert (
            await submit({"downloads": "", "preparations": "", "capacity_group": ""})
        ).status_code == 303
        addon = (await sources.stored())[0]
        assert (addon.downloads, addon.preparations, addon.capacity_group) == (None, None, "")
        assert (await submit({"downloads": "-1", "preparations": "3"})).status_code == 400
        assert "downloads" in refused[-1]["problems"]
        assert (await sources.stored())[0].preparations is None
        assert (await submit({"capacity_group": "x" * 101})).status_code == 400
        assert (await sources.stored())[0].capacity_group == ""

        # Both pages were current when opened. The save lock lets one update win;
        # the other sees the changed version instead of overwriting its limit.
        addon = (await sources.stored())[0]
        version = dashboard._addon_version(addon)
        outcomes: list[int] = []

        async def save(limit: str) -> None:
            outcomes.append((await submit({"downloads": limit}, version)).status_code)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(save, "2")
            tasks.start_soon(save, "3")
        assert sorted(outcomes) == [303, 409]
        assert (await sources.stored())[0].downloads in (2, 3)
    finally:
        await dashboard.aclose()
        await sources.aclose()
        await store.close()
