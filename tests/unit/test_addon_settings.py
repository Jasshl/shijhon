"""Add-ons kept by the configuration file: synchronized into the stored list at startup."""

from __future__ import annotations

from pathlib import Path

import pytest

from shijhon.config import load_settings
from shijhon.delivery.netpolicy import Reach
from shijhon.delivery.pacing import Limits
from shijhon.delivery.sources import SourceRegistry
from shijhon.store import Store


def _addons(*items: dict[str, object]) -> list[dict[str, object]]:
    return [{"base_url": f"https://{i['name']}.example.invalid/cfg", **i} for i in items]


def test_addons_are_optional_and_their_urls_stay_secret(tmp_path: Path) -> None:
    assert load_settings(None).addons is None
    config = tmp_path / "shijhon.toml"
    config.write_text(
        '[[addons]]\nname = "One"\nbase_url = "https://one.example.invalid/token-123"\n'
        'reach = "loopback"\nbudget_seconds = 50\nsettings = { quality = "5" }\n'
    )
    settings = load_settings(config)
    assert settings.addons is not None
    (addon,) = settings.addons
    assert (addon.name, addon.reach, addon.budget_seconds) == ("One", "loopback", 50)
    assert addon.settings == {"quality": "5"}
    assert "token-123" not in repr(settings) and "token-123" not in str(addon)


@pytest.mark.parametrize(
    "addons",
    [
        _addons({"name": "Same"}, {"name": "Same"}),
        _addons({"name": "Bad reach", "reach": "anywhere"}),
        _addons({"name": "Budget", "budget_seconds": 0}),
        _addons({"name": ""}),
        _addons({"name": "Typo", "budget_second": 5}),
        _addons({"name": "Rate", "requests_per_second": -1}),
        _addons({"name": "Burst", "request_burst": 0}),
        _addons({"name": "Openings", "audio_openings": 2000}),
    ],
)
def test_invalid_addons_are_refused(addons: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError):
        load_settings(None, addons=addons)


@pytest.mark.parametrize(
    "addons",
    [
        [{"name": "A", "base_url": "https://a.invalid/SECRET-1"}] * 2,  # a model check
        [{"base_url": "https://a.invalid/SECRET-1"}],  # a field check
    ],
)
def test_errors_do_not_repeat_the_configuration(addons: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError) as caught:
        load_settings(None, addons=addons, navidrome={"password": "SECRET-2"})
    assert "SECRET" not in str(caught.value)


async def _stored(store: Store) -> list[tuple[str, int, int, str, float | None, str]]:
    rows = await store.fetchall(
        "SELECT name, enabled, position, reach, budget_seconds, base_url FROM sources"
        " ORDER BY position, id"
    )
    return [
        (r["name"], r["enabled"], r["position"], r["reach"], r["budget_seconds"], r["base_url"])
        for r in rows
    ]


@pytest.mark.anyio
async def test_sync_adds_updates_orders_and_disables(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        manual = await sources.add("Manual", "https://manual.example.invalid/x")
        first = load_settings(
            None,
            addons=_addons(
                {"name": "A", "reach": "loopback", "budget_seconds": 50},
                {"name": "B"},
            ),
        )
        assert first.addons is not None
        await sources.sync(first.addons)
        assert await _stored(store) == [
            ("A", 1, 1, "loopback", 50, "https://A.example.invalid/cfg"),
            ("B", 1, 2, "public", None, "https://B.example.invalid/cfg"),
            ("Manual", 0, 3, "public", None, "https://manual.example.invalid/x"),
        ]
        ids = {s.name: s.id for s in await sources.enabled()}

        # Reordered, one updated (budget cleared, new URL), one dropped, one new.
        second = load_settings(
            None,
            addons=[
                {"name": "B", "base_url": "https://b2.example.invalid/cfg", "reach": "private"},
                {"name": "C", "base_url": "https://c.example.invalid/cfg"},
                {"name": "Manual", "base_url": "https://manual.example.invalid/x"},
            ],
        )
        assert second.addons is not None
        await sources.sync(second.addons)
        assert await _stored(store) == [
            ("B", 1, 1, "private", None, "https://b2.example.invalid/cfg"),
            ("C", 1, 2, "public", None, "https://c.example.invalid/cfg"),
            ("Manual", 1, 3, "public", None, "https://manual.example.invalid/x"),
            ("A", 0, 4, "loopback", 50, "https://A.example.invalid/cfg"),
        ]
        enabled = await sources.enabled()
        assert [s.name for s in enabled] == ["B", "C", "Manual"]
        assert enabled[0].id == ids["B"] and enabled[0].reach is Reach.PRIVATE
        assert enabled[2].id == manual  # history (IDs) kept for existing add-ons

        await sources.sync([])  # an empty list disables everything
        assert await sources.enabled() == []
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_sync_with_a_duplicate_stored_name(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        first = await sources.add("A", "https://a1.example.invalid/x")
        second = await sources.add("A", "https://a2.example.invalid/x")  # e.g. from a dashboard
        settings = load_settings(None, addons=_addons({"name": "A"}))
        assert settings.addons is not None
        await sources.sync(settings.addons)
        enabled = await sources.enabled()
        assert [s.id for s in enabled] == [first]  # the other one is disabled, placed after
        rows = await store.fetchall("SELECT id, enabled, position FROM sources ORDER BY id")
        assert [(r["id"], r["enabled"], r["position"]) for r in rows] == [
            (first, 1, 1),
            (second, 0, 2),
        ]
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_what_the_configurations_list_would_change_is_named(tmp_path: Path) -> None:
    """The file's [[addons]] no longer applied: what differs is told by add-on name,
    never by value."""
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        listed = load_settings(
            None,
            addons=_addons({"name": "A", "settings": {"quality": "6"}}, {"name": "B"}),
        ).addons
        assert listed is not None
        await sources.sync(listed)
        assert await sources.differs(listed) == []
        ids = {s.name: s.id for s in await sources.enabled()}
        await sources.update(ids["A"], budget_seconds=7)  # changed in the dashboard
        assert await sources.differs(listed) == ["A"]
        await sources.update(ids["A"], clear_budget=True, settings={"quality": 6})
        assert await sources.differs(listed) == ["A"]  # a number is not the file's text
        await sources.update(ids["A"], settings={"quality": "6"})
        await sources.reorder([ids["B"], ids["A"]])
        assert await sources.differs(listed) == ["the list"]
        await sources.reorder([ids["A"], ids["B"]])
        await sources.set_enabled(ids["B"], False)
        assert await sources.differs(listed) == ["the list"]
        await sources.set_enabled(ids["B"], True)
        await sources.update(ids["B"], base_url="https://elsewhere.example.invalid/key")
        found = await sources.differs(listed)
        assert found == ["B"] and "elsewhere" not in " ".join(found)
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_the_files_own_limits_of_an_addon_are_synchronized(tmp_path: Path) -> None:
    """``[[addons]] requests_per_second``, ``request_burst`` and ``audio_openings`` -
    an add-on's own limits on what Shijhon sends it; not given: the installation's."""
    config = tmp_path / "shijhon.toml"
    config.write_text(
        "[delivery]\naddon_requests_per_second = 3\n\n"
        '[[addons]]\nname = "Mine"\nbase_url = "https://mine.example.invalid/cfg"\n'
        "requests_per_second = 20\nrequest_burst = 10\naudio_openings = 0\n\n"
        '[[addons]]\nname = "Theirs"\nbase_url = "https://theirs.example.invalid/cfg"\n'
    )
    settings = load_settings(config)
    assert settings.addons is not None
    delivery = settings.delivery
    assert (delivery.addon_requests_per_second, delivery.addon_request_burst) == (3.0, 4)
    assert delivery.addon_audio_openings == 4  # the built-in default
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store, limits=Limits(3.0, 4, 4))
    try:
        await sources.sync(settings.addons)
        assert await sources.differs(settings.addons) == []
        mine, theirs = await sources.enabled()
        assert mine.pace is not None and mine.pace.limits == Limits(20.0, 10, 0)
        assert theirs.pace is not None and theirs.pace.limits == Limits(3.0, 4, 4)
        # Changed in the dashboard: the file's differ (named) - and apply again when
        # the list is the file's (a sync).
        await sources.update(theirs.id, limits=Limits(1.0, None, None))
        assert await sources.differs(settings.addons) == ["Theirs"]
        await sources.sync(settings.addons)
        assert [s.limits for s in await sources.stored()] == [Limits(20.0, 10, 0), Limits()]
    finally:
        await sources.aclose()
        await store.close()
