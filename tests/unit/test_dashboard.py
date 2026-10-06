"""Dashboard pieces without a Navidrome: numbers, generated fields, the precedence
rule, manifests, sign-in limits, admin checks and recent errors."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field, SecretStr

from shijhon.app import ShijhonApp
from shijhon.config import DeliverySettings, Settings, catalog_settings, load_settings
from shijhon.dashboard import sections
from shijhon.dashboard.addons import parse_manifest
from shijhon.dashboard.auth import AdminCheck, Attempts, Sessions, client_key
from shijhon.dashboard.errors import RecentErrors
from shijhon.dashboard.fields import (
    Group,
    Invalid,
    format_number,
    parse_number,
    section_fields,
    spoken_unit,
)
from shijhon.dashboard.live import LIVE
from shijhon.dashboard.saved import (
    ADAPTER,
    REMOVE,
    Saved,
    SavedSettings,
    effective,
    hidden_by_environment,
    locked_keys,
)
from shijhon.delivery.netpolicy import Reach
from shijhon.delivery.sources import StoredSource
from shijhon.navidrome.client import NavidromeError
from shijhon.proxy.forwarding import Network, client_address, networks
from shijhon.store import Store


def key(peer: str, forwarded_for: list[str], trusted: list[Network]) -> str:
    """The sign-in limit's key for a request from ``peer`` with these X-Forwarded-For lines."""
    headers = [(b"x-forwarded-for", line.encode()) for line in forwarded_for]
    return client_key(client_address(peer, headers, trusted), peer)


# --- numbers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "whole", "value"),
    [
        ("4,5", False, 4.5),
        ("4.5", False, 4.5),
        (" 4.5 ", False, 4.5),
        ("9", False, 9.0),
        (".5", False, 0.5),
        ("1 800", False, 1800.0),
        ("3", True, 3),
        ("3.0", True, 3),
        ("-2", True, -2),
    ],
)
def test_numbers_take_a_comma_or_a_dot(typed: str, whole: bool, value: float) -> None:
    assert parse_number(typed, whole=whole) == value


@pytest.mark.parametrize(
    ("typed", "whole"),
    [
        ("abc", False),
        ("", False),
        ("1,000.5", False),
        ("1e3", False),
        ("nan", False),
        ("4,5", True),
        ("2.5", True),
        ("1,2,3", False),
    ],
)
def test_other_text_is_not_a_number(typed: str, whole: bool) -> None:
    with pytest.raises(Invalid):
        parse_number(typed, whole=whole)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (4.5, "4.5"),
        (9.0, "9"),
        (0.8, "0.8"),
        (1800.0, "1800"),
        (30, "30"),
        (0.15, "0.15"),
        (0.0, "0"),
    ],
)
def test_values_and_defaults_share_one_format(value: float, text: str) -> None:
    assert format_number(value) == text


# --- fields from the definitions --------------------------------------------------------


def _fields(section: str = "delivery", kind: str = "none") -> dict[str, Any]:
    return {f.key: f for f in sections.fields_of(section, kind)}


def test_every_setting_of_a_section_has_a_field() -> None:
    assert set(_fields()) == set(DeliverySettings.model_fields)
    fields = _fields()
    assert fields["budget_seconds"].kind == "decimal" and fields["budget_seconds"].unit == "s"
    assert fields["warm_ahead_depth"].kind == "whole"
    assert fields["prepare_when_not_ready"].kind == "switch"
    assert fields["routing"].kind == "select"
    assert fields["routing"].options()[0] == ("ordered", "In order")
    timeouts = fields["primary_cooldown_timeouts"]
    assert (timeouts.low, timeouts.low_open) == (1.0, False)
    with pytest.raises(Invalid, match=r"^Enter a whole number of at least 1\.$"):
        timeouts.parse("0")
    assert fields["max_wait_seconds"].parse("30") == 30.0
    with pytest.raises(Invalid, match="above 0"):
        fields["max_wait_seconds"].parse("0")
    assert "token" not in _fields("catalog")  # an adapter's setting: with its kind only
    catalog = _fields("catalog", "sample")
    assert set(catalog) == set(catalog_settings("sample").model_fields)
    assert catalog["kind"].kind == "select"
    assert ("sample", "Sample catalog") in catalog["kind"].options()
    assert catalog["token"].kind == "secret" and catalog["token_file"].kind == "path"
    assert catalog["headers"].kind == "mapping" and not catalog["headers"].editable
    with pytest.raises(Invalid, match="two letters"):
        catalog["region"].parse("g1")


def test_a_setting_added_elsewhere_appears_without_dashboard_code() -> None:
    class Later(BaseModel):
        budget_seconds: float = 9.0
        new_wait_minutes: float = Field(default=5.0, ge=1, le=60, description="Waits a while.")
        new_mode: Literal["fast", "slow_down"] = "fast"

    fields = {f.key: f for f in section_fields("delivery", Later)}
    added = fields["new_wait_minutes"]
    assert (added.meta.label, added.meta.help, added.unit, added.spoken) == (
        "New wait",
        "Waits a while.",
        "min",
        "minutes",
    )
    assert added.meta.group == "advanced"  # the section's last group
    assert added.rule() == "from 1 to 60" and added.describe(5.0) == "5 min"
    assert fields["new_mode"].options() == [("fast", "Fast"), ("slow_down", "Slow down")]
    assert ("delivery", "new_wait_minutes") not in LIVE  # so it says "applies after restart"


def test_more_settings_open_only_for_a_row_that_needs_to_be_seen() -> None:
    """Collapsed unless one of its rows has a problem (also the second end of a range),
    was saved just now, or a notice links to it; the groups shown first never open it."""
    first, more = Group("first", "First"), Group("more", "More", more=True)

    def row(name: str, **values: Any) -> dict[str, Any]:
        return {"name": name, "id": f"x-{name}", "problem": None, "saved": False, **values}

    def fold(*rows: dict[str, Any], **kwargs: Any) -> sections.Fold:
        shown, found = sections.folded(
            [(first, [row("a", problem="Enter a number.", saved=True)]), (more, list(rows))],
            **kwargs,
        )
        assert shown == [(first, [row("a", problem="Enter a number.", saved=True)])]
        return found

    assert not fold(row("b"), row("c")).open
    assert fold(row("b"), row("c", problem="Enter a number.")).open
    assert fold(row("b", pair=row("c", problem="Must be at most the other end."))).open
    assert fold(row("b", saved=True)).open
    assert fold(row("b"), linked=frozenset({"x-b"})).open
    assert fold(row("b"), problems={"b": None}).open  # a first step's, not in the row
    assert not fold(row("b"), problems={"a": None}).open
    assert fold(row("b", modified=True), row("c"), row("d", modified=True)).changed == 2
    assert sections.folded([(more, [])])[1].groups == []  # an empty group is left out


def test_a_unit_the_label_says_already_is_not_spoken_again() -> None:
    assert spoken_unit("Requests a second", "requests a second") == ""
    assert spoken_unit("Songs opened at once", "songs") == ""
    assert spoken_unit("Requests at once", "Requests") == ""
    assert spoken_unit("Time to first audio", "seconds") == "seconds"
    assert spoken_unit("Request rate", "requests per second") == "requests per second"
    assert spoken_unit("Kept for", "days") == "days"


# --- the precedence rule ----------------------------------------------------------------


def _saved(**values: Any) -> dict[str, dict[str, Saved]]:
    return {"delivery": {k: Saved(v, 0.0, "tester") for k, v in values.items()}}


@pytest.fixture(autouse=True)
def _no_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's or CI's own SHIJHON_* variables must not lock settings here."""
    for name in list(os.environ):
        if name.upper().startswith("SHIJHON_"):
            monkeypatch.delenv(name)


def _locked(**overrides: Any) -> tuple[Settings, dict[str, dict[str, str]]]:
    configured = load_settings(None, **overrides)
    return configured, locked_keys(configured)


def _row(groups: Any, name: str) -> dict[str, Any]:
    return next(r for _, rows in groups for r in rows if r["name"] == name)


def test_a_list_of_sizes_is_typed_as_numbers() -> None:
    """``[catalog] cover_sizes``: "100, 150, 200"; empty: none (the exact size)."""
    from shijhon.config import CatalogSettings

    [sizes] = [f for f in section_fields("catalog", CatalogSettings) if f.key == "cover_sizes"]
    assert sizes.kind == "numbers" and sizes.editable
    assert sizes.parse("600, 100 ;300") == [100, 300, 600] and sizes.parse("") == []
    assert sizes.show([100, 300]) == "100, 300" and sizes.describe([]) == "none"
    for typed in ("100, big", "0", "-5", "1.5", "\u00b2"):
        with pytest.raises(Invalid):
            sizes.parse(typed)
    # The setting keeps them in order, each once, from 1 to 1200 px (file and dashboard agree).
    kept = load_settings(None, catalog={"cover_sizes": [300, 100, 300]}).catalog
    assert kept.cover_sizes == [100, 300]
    with pytest.raises(ValueError, match="1 to 1200"):
        load_settings(None, catalog={"cover_sizes": [1300]})


def test_default_then_file_then_dashboard_then_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "shijhon.toml"
    config.write_text("[delivery]\nbudget_seconds = 7\ncooldown_seconds = 40\n")
    monkeypatch.setenv("SHIJHON_DELIVERY__COOLDOWN_SECONDS", "50")
    configured = load_settings(config)
    locked = locked_keys(configured)
    assert locked == {"delivery": {"cooldown_seconds": "SHIJHON_DELIVERY__COOLDOWN_SECONDS"}}
    assert configured.delivery.budget_seconds == 7  # the file over the built-in 9
    saved = _saved(budget_seconds=8.0, cooldown_seconds=60)
    applying, problems = effective(configured, saved, locked=locked)
    assert problems == {}
    assert applying.delivery.budget_seconds == 8  # the dashboard over the file
    assert applying.delivery.cooldown_seconds == 50  # the environment over the dashboard
    assert applying.delivery.seek_timeout_seconds == 15  # nothing set: the built-in default
    assert configured.delivery.budget_seconds == 7  # untouched
    assert hidden_by_environment(saved, locked) == ["delivery.cooldown_seconds"]


def test_the_environment_is_read_as_the_settings_read_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("shijhon_delivery__routing", "primary_first")  # any letter case
    monkeypatch.setenv("SHIJHON_CATALOG", '{"kind": "sample", "region": "gb"}')  # as JSON
    monkeypatch.setenv("SHIJHON_SERVER__CREDENTIAL_CACHE_SECONDS", "2")  # no dashboard section
    monkeypatch.setenv("SHIJHON_DELIVERY__RELIABLE_SOURCE", "")  # empty: sets nothing
    monkeypatch.setenv("SHIJHON_DELIVERY__COOLDOWN_SECONDS", "5")
    configured, locked = _locked(delivery={"cooldown_seconds": 7})  # an override wins
    assert configured.delivery.cooldown_seconds == 7 and configured.delivery.reliable_source == ""
    assert locked == {
        "delivery": {"routing": "shijhon_delivery__routing"},  # the variable as it is named
        "catalog": {
            "kind": "SHIJHON_CATALOG (JSON)",
            "region": "SHIJHON_CATALOG (JSON)",
        },
    }


def test_the_catalog_token_sources_are_locked_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("from-the-file")
    monkeypatch.setenv("SHIJHON_CATALOG__KIND", "sample")
    monkeypatch.setenv("SHIJHON_CATALOG__TOKEN_FILE", str(token_file))
    configured, locked = _locked()
    assert set(locked["catalog"]) == {"kind", "token", "token_file", "token_url"}
    saved = {
        "catalog": {
            "token": Saved("from-the-dashboard", 0.0, "t"),
            ADAPTER: Saved("sample", 0.0, "t"),
        }
    }
    applying, _ = effective(configured, saved, locked=locked)
    assert applying.catalog.secret_token() == "from-the-file"  # not the dashboard's token
    token = _row(
        sections.rows("catalog", configured, saved, addon_names=[], locked=locked), "token"
    )
    assert token["kind"] == "locked" and token["text"] == "Not set" and token["hidden"]
    assert token["locked"] == "SHIJHON_CATALOG__TOKEN_FILE"


def test_settings_set_by_the_environment_are_shown_and_never_taken_from_a_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anyio

    monkeypatch.setenv("SHIJHON_DELIVERY__COOLDOWN_SECONDS", "50")
    monkeypatch.setenv("SHIJHON_DELIVERY__ROUTING", "primary_first")
    monkeypatch.setenv("SHIJHON_DELIVERY__PREPARE_WHEN_NOT_READY", "true")
    monkeypatch.setenv("SHIJHON_CATALOG__KIND", "sample")
    monkeypatch.setenv("SHIJHON_CATALOG__TOKEN", "from-env")
    configured, locked = _locked()
    saved = _saved(cooldown_seconds=45.0)
    # A failed save re-renders from the form; locked rows keep the environment's values.
    form = {"cooldown_seconds": "999", "routing": "", "budget_seconds": "abc"}
    groups = sections.rows("delivery", configured, saved, addon_names=[], form=form, locked=locked)
    cooldown = _row(groups, "cooldown_seconds")
    assert (cooldown["kind"], cooldown["text"], cooldown["hidden"]) == ("locked", "50 s", True)
    assert cooldown["locked"] == "SHIJHON_DELIVERY__COOLDOWN_SECONDS"
    assert not cooldown["restart"] and not cooldown["default"]
    assert _row(groups, "routing")["text"] == "Primary first"
    assert _row(groups, "prepare_when_not_ready")["text"] == "On"
    token = _row(sections.rows("catalog", configured, {}, addon_names=[], locked=locked), "token")
    assert token["text"] == "Set" and "from-env" not in str(token)

    async def run(form: dict[str, str]) -> sections.Written:
        submitted = await sections.submit(
            "delivery", configured, saved, form, addon_names=[], locked=locked
        )
        assert not submitted.problems
        return sections.writes("delivery", configured, saved, submitted, locked=locked)

    written = anyio.run(run, {"cooldown_seconds": "5", "seek_timeout_seconds": "20"})
    assert "cooldown_seconds" not in written.writes  # a form cannot set it, nor remove it
    assert written.writes == {"seek_timeout_seconds": 20.0}
    removed = anyio.run(run, {"cooldown_seconds-clear": "true"})  # only on request
    assert removed.writes == {"cooldown_seconds": REMOVE} and removed.removed == [
        "cooldown_seconds"
    ]
    assert removed.changed == []  # the environment's value applies either way


def test_the_first_step_s_page_is_the_refused_page_without_its_marks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A form that chooses another catalog whose settings are needed is the first of two
    steps, not a mistake. The page for that catalog has the fields, values and states a
    refused save shows - a secret that is set, a value from the configuration, one from
    the environment - and no mark of a problem. With the catalog's settings on the form
    the same problem is a refusal."""
    import anyio

    monkeypatch.setenv("SHIJHON_CATALOG__REQUESTS_PER_SECOND", "3")
    sample = {"kind": "sample", "token": "file-token", "region": "zzz"}
    configured, locked = _locked(catalog=sample)
    saved = {"catalog": {"kind": Saved("demo", 0.0, "tester")}}  # the demo is in use

    def submit(form: dict[str, str]) -> sections.Submitted:
        async def run() -> sections.Submitted:
            return await sections.submit(
                "catalog", configured, saved, form, addon_names=[], locked=locked
            )

        return anyio.run(run)

    def rows(form: dict[str, str], problems: Any, **more: Any) -> list[dict[str, Any]]:
        groups = sections.rows(
            "catalog", configured, saved, addon_names=[], form=form, problems=problems,
            locked=locked, **more,
        )  # fmt: skip
        return [row for _, members in groups for row in members]

    form = {"kind": "sample", sections.SHOWN_KIND: "demo"}
    chosen = submit(form)
    assert chosen.first_step and list(chosen.problems) == ["region"]
    refused = rows(form, chosen.problems)
    first = rows(form, chosen.problems, unmarked=True)
    assert [row["name"] for row in refused if row["problem"]] == ["region"]
    assert all(row["problem"] is None for row in first)
    assert [{**row, "problem": None} for row in refused] == first  # nothing else differs
    by_name = {row["name"]: row for row in first}
    assert by_name["token"]["state"] == ("set", "From the configuration")
    assert by_name["region"]["text"] == "zzz"
    rate = by_name["requests_per_second"]
    assert (rate["kind"], rate["text"]) == ("locked", "3 per s")
    assert rate["locked"] == "SHIJHON_CATALOG__REQUESTS_PER_SECOND"
    assert "file-token" not in repr(first)
    # The second step: the catalog's settings were on the form.
    again = submit({"kind": "sample", sections.SHOWN_KIND: "sample", "region": "zzz"})
    assert not again.first_step and list(again.problems) == ["region"]
    # A mistake in one of Shijhon's own settings is one at either step.
    wrong = submit({**form, "cache_seconds": "soon"})
    assert not wrong.first_step and list(wrong.problems) == ["cache_seconds"]
    # Nothing to enter: saved at the first step.
    fine = submit({"kind": "demo", sections.SHOWN_KIND: "sample"})
    assert not fine.problems and not fine.first_step


def test_problems_land_on_settings_that_can_be_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio

    monkeypatch.setenv("SHIJHON_DELIVERY__MAX_WAIT_SECONDS", "30")
    configured, locked = _locked()

    async def submit(section: str, form: dict[str, str]) -> sections.Submitted:
        return await sections.submit(section, configured, {}, form, addon_names=[], locked=locked)

    too_long = anyio.run(submit, "delivery", {"budget_seconds": "40"})
    assert set(too_long.problems) == {"budget_seconds"}  # the wait cap is the environment's
    assert too_long.problems["budget_seconds"].row.startswith(
        "Must be at most the longest wait (30 s)"
    )
    monkeypatch.setenv("SHIJHON_CATALOG__KIND", "sample")
    monkeypatch.setenv("SHIJHON_CATALOG__TOKEN", "t")
    monkeypatch.setenv("SHIJHON_CATALOG__REGION", "g1")
    configured, locked = _locked()
    bad = anyio.run(submit, "catalog", {"artwork_size": "600"})
    problem = bad.problems["region"]
    assert "SHIJHON_CATALOG__REGION" in problem.row and "environment" in problem.notice


def test_the_default_trusted_proxy_is_loopback_only() -> None:
    default = load_settings(None).server.trusted_proxies
    assert default == ["127.0.0.0/8", "::1/128"]
    # A proxy on Docker's network is not trusted until added: its clients share one count.
    assert key("172.18.0.1", ["1.2.3.4"], networks(default)) == "172.18.0.1"


def test_saving_the_configurations_value_removes_the_saved_one() -> None:
    configured = load_settings(None, delivery={"budget_seconds": 7})
    saved = _saved(budget_seconds=8.0)
    form = {"budget_seconds": "7", "cooldown_seconds": "30"}

    async def run() -> sections.Written:
        submitted = await sections.submit(
            "delivery", configured, saved, form, addon_names=[], locked={}
        )
        assert not submitted.problems
        return sections.writes("delivery", configured, saved, submitted, locked={})

    import anyio

    written = anyio.run(run)
    assert written.writes == {"budget_seconds": REMOVE}
    assert written.changed == ["budget_seconds"]


def test_saved_values_that_do_not_fit_keep_the_configuration() -> None:
    configured = load_settings(None, delivery={"budget_seconds": 12})
    applying, problems = effective(configured, _saved(max_wait_seconds=10.0), locked={})
    assert applying.delivery.max_wait_seconds == 30  # the section falls back as a whole
    assert "max_wait_seconds" in problems["delivery"] and "12" in problems["delivery"]


def test_secret_problems_never_repeat_the_value() -> None:
    configured = load_settings(None, catalog={"kind": "sample"})
    saved = {
        "catalog": {
            "requests_per_second": Saved("fast-SECRET", 0.0, "t"),
            "token": Saved("tok-SECRET", 0.0, "t"),
            ADAPTER: Saved("sample", 0.0, "t"),
        }
    }
    _, problems = effective(configured, saved, locked={})
    assert "catalog" in problems and "SECRET" not in problems["catalog"]


@pytest.mark.anyio
async def test_startup_uses_the_saved_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIJHON_DELIVERY__SEEK_TIMEOUT_SECONDS", "50")
    settings = _settings(
        tmp_path,
        delivery={"budget_seconds": 7, "primary_budget_seconds": 3},
        catalog={"kind": "sample", "token": "from-the-file"},
    )
    store = await Store.open(settings.database_path)
    await SavedSettings(store).put(
        "delivery",
        {"budget_seconds": 8.0, "cooldown_seconds": 45.0, "seek_timeout_seconds": 5.0},
        "tester",
    )
    await SavedSettings(store).put(
        "catalog", {"token": SecretStr("tok-1"), ADAPTER: "sample"}, "tester"
    )
    await store.close()
    app = ShijhonApp(settings)
    await app.startup()
    try:
        assert app.services is not None
        deliverer = app.services.deliverer.settings
        assert (deliverer.budget_seconds, deliverer.cooldown_seconds) == (8, 45)
        assert deliverer.primary_budget_seconds == 3  # the configuration's
        assert deliverer.seek_timeout_seconds == 50  # the environment's, over the saved 5
        assert app.settings.catalog.token is not None
        assert app.settings.catalog.token.get_secret_value() == "tok-1"
        assert app.configured.delivery.budget_seconds == 7
    finally:
        await app.shutdown()


@pytest.mark.anyio
async def test_the_addon_list_is_the_dashboards_once_changed_there(tmp_path: Path) -> None:
    addons = [{"name": "One", "base_url": "https://one.example.invalid/cfg"}]
    settings = _settings(tmp_path, addons=addons)

    async def names(app: ShijhonApp) -> list[str]:
        assert app.services is not None
        return [s.name for s in await app.services.sources.enabled()]

    app = ShijhonApp(settings)
    await app.startup()
    assert app.services is not None
    await app.services.sources.add("Two", "https://two.example.invalid/x")
    await app.services.sources.reorder([2, 1])
    assert app.dashboard.saved is not None
    await app.dashboard.saved.take_addons("tester")
    await app.shutdown()

    app = ShijhonApp(settings)  # the file still lists only One
    await app.startup()
    assert await names(app) == ["Two", "One"]
    assert app.dashboard.saved is not None
    await app.dashboard.saved.hand_back_addons("tester")
    await app.shutdown()

    app = ShijhonApp(settings)
    await app.startup()
    assert await names(app) == ["One"]  # the file's list again
    await app.shutdown()


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return load_settings(
        None,
        state_dir=tmp_path / "state",
        navidrome={"url": "http://127.0.0.1:9", "library_path": tmp_path},
        **overrides,
    )


# --- manifests ----------------------------------------------------------------------------


def test_manifest_settings_become_fields() -> None:
    info = parse_manifest(
        {
            "name": "Alpha",
            "version": 2,
            "settings": [
                {
                    "key": "quality",
                    "label": "Quality",
                    "type": "select",
                    "default": 6,
                    "options": [5, {"value": "6", "label": "FLAC CD"}],
                },
                {"key": "preferOpus", "type": "toggle", "default": True},
                {"key": "apiKey", "type": "text"},
                {"key": "session", "label": "Session", "type": "password"},
                {"key": "region", "type": "text", "default": "eu", "secret": False},
                {"key": "note", "type": "text", "default": "n"},
                {"key": "q", "type": "text"},
                "not a setting",
            ],
        }
    )
    assert (info.name, info.version) == ("Alpha", "2")
    kinds = {item.key: (item.kind, item.default) for item in info.declared}
    assert kinds == {
        "quality": ("select", "6"),
        "preferOpus": ("switch", "true"),
        "apiKey": ("secret", None),
        "session": ("secret", None),
        "region": ("text", "eu"),
        "note": ("secret", "n"),  # free text is write-only unless the manifest marks it plain
    }
    assert info.declared[0].options == (("5", "5"), ("6", "FLAC CD"))


# --- sign-in limits, admin checks, recent errors ------------------------------------------


def test_sign_in_attempts_are_limited_per_client() -> None:
    now = [0.0]
    attempts = Attempts(limit=3, window=60.0, clients=3, clock=lambda: now[0])
    trusted = networks(["127.0.0.0/8", "10.0.0.0/8"])
    one, other = key("203.0.113.1", [], trusted), key("203.0.113.2", [], trusted)
    assert [attempts.allow(one) for _ in range(4)] == [True, True, True, False]
    assert attempts.allow(other)  # someone else is not locked out
    now[0] = 61
    assert attempts.allow(one)
    # Past the number of clients told apart, new ones share one count.
    for n in range(3, 10):
        attempts.allow(f"198.51.100.{n}")
    assert len(attempts._times) <= 4


def test_only_a_trusted_proxy_names_the_client() -> None:
    trusted = networks(["10.0.0.0/8"])
    # Behind a trusted proxy: the last address it added (all header lines together).
    assert key("10.0.0.9", ["6.6.6.6, 1.2.3.4"], trusted) == "1.2.3.4"
    assert key("10.0.0.9", ["6.6.6.6", "1.2.3.4"], trusted) == "1.2.3.4"
    # Anyone else: the peer, whatever the header claims.
    assert key("203.0.113.7", ["1.2.3.4"], trusted) == "203.0.113.7"
    assert key("10.0.0.9", ["not an address"], trusted) == "10.0.0.9"
    # IPv6 per /64.
    assert key("2001:db8::1", [], trusted) == key("2001:db8::2", [], trusted)


@pytest.mark.anyio
async def test_sessions_end_when_old_or_unused(tmp_path: Path) -> None:
    now = [1000.0]
    store = await Store.open(tmp_path / "state.sqlite3")
    sessions = Sessions(store, lifetime=100.0, idle=30.0, clock=lambda: now[0])
    try:
        token, _ = await sessions.create("mira")
        now[0] += 25
        assert await sessions.get(token) is not None
        now[0] += 25  # 50 s after sign-in, 25 s after use... but use is noted every 5 min
        assert await sessions.get(token) is None
        token, _ = await sessions.create("mira")
        now[0] += 101
        assert await sessions.get(token) is None
        assert await sessions.get("made-up") is None
    finally:
        await store.close()


class _Navidrome:
    def __init__(self) -> None:
        self.users: list[dict[str, Any]] | None = [{"userName": "Mira", "isAdmin": True}]
        self.asked = 0

    async def native_json(self, method: str, path: str) -> Any:
        self.asked += 1
        if self.users is None:
            raise NavidromeError("native GET: ConnectError")
        return self.users


@pytest.mark.anyio
async def test_admin_rights_are_checked_again_and_fail_closed() -> None:
    now = [0.0]
    navidrome: Any = _Navidrome()
    check = AdminCheck(every=60, grace=600, clock=lambda: now[0])
    assert await check.admin(navidrome, "mira") is True
    assert await check.admin(navidrome, "mira") is True and navidrome.asked == 1  # reused
    navidrome.users = None  # Navidrome stops answering
    now[0] = 100
    assert await check.admin(navidrome, "mira") is True  # confirmed 100 s ago: the grace
    now[0] = 700
    assert await check.admin(navidrome, "mira") is None  # refused, not signed out
    assert await check.admin(navidrome, "someone") is None  # never confirmed: refused
    navidrome.users = [{"userName": "mira", "isAdmin": False}]
    assert await check.admin(navidrome, "mira") is False
    navidrome.users = []
    now[0] = 800
    assert await check.admin(navidrome, "mira") is False  # no such user any more


def test_recent_errors_are_grouped_and_redacted() -> None:
    now = [1000.0]
    recent = RecentErrors(clock=lambda: now[0])
    logger = logging.getLogger("shijhon.delivery.test_recent")
    logger.addHandler(recent)
    try:
        for _ in range(3):
            logger.warning("stream failed at https://addon.example/k3y/stream?token=abc")
        logger.info("not an error")
        now[0] += 90000
        logging.getLogger("shijhon.catalog.x").addHandler(recent)
        logging.getLogger("shijhon.catalog.x").error("search: HTTP 429")
    finally:
        logger.removeHandler(recent)
        logging.getLogger("shijhon.catalog.x").removeHandler(recent)
    rows = recent.recent()
    assert [(r.where, r.message, r.times) for r in rows] == [
        ("Catalog", "search: HTTP 429", 1)
    ]  # the playback one is older than a day
    now[0] = 1000.0
    recent2 = RecentErrors(clock=lambda: now[0])
    recent2.emit(
        logging.makeLogRecord(
            {
                "name": "shijhon.delivery.p",
                "levelno": 30,
                "msg": "at https://addon.example/k3y/x?token=abc",
            }
        )
    )
    (row,) = recent2.recent()
    assert "k3y" not in row.message and "abc" not in row.message and row.where == "Playback"


@pytest.mark.parametrize("value", [SecretStr("x"), None])
def test_secrets_are_stored_as_their_text(value: SecretStr | None) -> None:
    from shijhon.dashboard.saved import encode

    assert encode(value) == ('"x"' if value is not None else "null")


@pytest.mark.anyio
async def test_the_command_line_lists_and_clears_saved_values(tmp_path: Path) -> None:
    from shijhon.dashboard.saved import clear_saved, list_saved

    database = tmp_path / "shijhon.sqlite3"
    store = await Store.open(database)
    saved = SavedSettings(store)
    await saved.put("delivery", {"budget_seconds": 8.0, "cooldown_seconds": 45.0}, "mira")
    await saved.put("catalog", {"token": SecretStr("tok-SECRET"), "region": "gb"}, "mira")
    await store.close()
    listing = list_saved(database)
    assert "delivery.budget_seconds = 8.0" in listing and "by mira" in listing
    assert "catalog.token = (secret)" in listing and "SECRET" not in listing
    assert clear_saved(database, ["catalog.token", "delivery"]) == 3
    assert list_saved(database).splitlines() == [
        next(line for line in listing.splitlines() if line.startswith("catalog.region"))
    ]


# --- live settings, manifests, secrets, the environment ---------------------------------


def _changed(value: Any, key: str) -> Any:
    """Another valid value for a setting."""
    if isinstance(value, bool):
        return not value
    if key == "routing":
        return "primary_first"
    if isinstance(value, str):
        return "Elsewhere"
    if isinstance(value, int):
        return value + 1
    return value * 2 + 1


@pytest.mark.anyio
async def test_every_live_setting_reaches_the_running_services(tmp_path: Path) -> None:
    """Each entry of the live table changes what the running services read."""
    from shijhon.dashboard import live

    app = ShijhonApp(_settings(tmp_path))
    await app.startup()
    try:
        services = app.services
        assert services is not None
        for (section, key), targets in live.LIVE.items():
            current = getattr(app.settings, section)
            values = current.model_copy(update={key: _changed(getattr(current, key), key)})
            if key == "budget_seconds":  # the wait cap must stay above it
                values = values.model_copy(update={"max_wait_seconds": 100.0})
            absent = [
                t
                for t in targets
                if t.live_if is not None and live.owner(services, t.path, optional=True) is None
            ]
            applied = await live.apply(services, section, [key], values)
            assert applied == ([] if absent else [key]), key  # no pass: after a restart
            for target in targets:
                found = live.owner(services, target.path, optional=target.optional)
                if found is None:
                    continue  # absent here (no catalog commits without a catalog)
                assert getattr(found[0], found[1]) == target.value(values), target.path
    finally:
        await app.shutdown()


@pytest.mark.anyio
async def test_a_live_setting_whose_attribute_is_gone_waits_for_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from shijhon.dashboard import live

    app = ShijhonApp(_settings(tmp_path))
    await app.startup()
    try:
        assert app.services is not None
        broken = {("delivery", "cooldown_seconds"): (live.Target("deliverer.gone", lambda s: 1),)}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(live, "LIVE", broken)
            applied = await live.apply(app.services, "delivery", ["cooldown_seconds"], None)
        assert applied == [] and "cannot apply at once" in caplog.text
    finally:
        await app.shutdown()


@pytest.mark.anyio
async def test_manifest_checks_are_bounded_and_survive_broken_addons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as clock

    import anyio

    from shijhon.dashboard import addons

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await anyio.sleep(3600)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RecursionError

    def addon(id: int, enabled: bool = True) -> StoredSource:
        return StoredSource(
            id, f"A{id}", f"https://a{id}.invalid", {}, enabled, id, Reach.PUBLIC, None
        )

    monkeypatch.setattr(addons, "CHECK_TIMEOUT", 0.2)
    checks = addons.ManifestChecks()
    monkeypatch.setattr(addons, "fetch_manifest", slow)
    started = clock.monotonic()
    await checks.check([addon(1)])
    assert clock.monotonic() - started < 3
    assert (known := checks.known(addon(1))) is not None and known.error == "timeout"
    monkeypatch.setattr(addons, "fetch_manifest", broken)
    await checks.check([addon(2)])
    assert (known := checks.known(addon(2))) is not None
    assert known.error == "unreadable manifest (RecursionError)"
    # A disabled add-on is read once (for its settings form), not every minute.
    checks.every = 0
    calls: list[int] = []

    async def counted(url: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return addons.ManifestInfo("", "", ())

    monkeypatch.setattr(addons, "fetch_manifest", counted)
    for _ in range(3):
        await checks.check([addon(3, enabled=False)])
    assert len(calls) == 1


@pytest.mark.anyio
async def test_a_manifest_check_takes_its_turn_at_the_addons_request_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard's manifest requests count at the add-on's limit like any request,
    after everything a listener waits for; one that gets no turn in time is not asked, and
    says nothing about the add-on (what was read before stands)."""
    import time as clock

    from shijhon.dashboard import addons
    from shijhon.delivery import pacing

    monkeypatch.setattr(addons, "CHECK_TIMEOUT", 0.3)
    monkeypatch.setattr(addons, "KNOWN_WAIT", 0.05)
    pace = pacing.AddonPace(pacing.Limits(0.01, 1, 0))  # a request every 100 s
    asked: list[pacing.Urgency | None] = []
    real = pace.request

    async def noted() -> None:
        asked.append(pacing.current())
        await real()

    monkeypatch.setattr(pace, "request", noted)
    seen: list[str] = []

    async def pace_at(url: str) -> pacing.AddonPace:
        seen.append(url)
        return pace

    async def nowhere(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    # The one request to be had: it goes (and fails at the address, which is not the point).
    with pytest.raises(ValueError, match="connection failed"):
        await addons.fetch_manifest(
            "https://a1.invalid/x", Reach.PUBLIC, resolver=nowhere, pace=pace_at
        )
    assert seen == ["https://a1.invalid/x/manifest.json"]  # where the request goes
    assert pace.sent == 1 and asked[0] is not None and asked[0].level == pacing.CHECK
    # The next gets no turn within the check's time: not asked.
    with pytest.raises(addons.Busy, match="waiting for the add-on's request limit"):
        await addons.fetch_manifest(
            "https://a1.invalid/x", Reach.PUBLIC, resolver=nowhere, pace=pace_at
        )
    assert pace.sent == 1

    addon = StoredSource(1, "A1", "https://a1.invalid", {}, True, 1, Reach.PUBLIC, None)
    checks = addons.ManifestChecks(every=0, pace=pace_at)
    read = addons.Check(1.0, addons.ManifestInfo("A1", "1.0", ()), None)
    checks._checks[addon.id] = (checks._key(addon), read)
    began = clock.monotonic()
    await checks.check([addon])
    assert checks.known(addon) is read  # kept as it was ...
    assert clock.monotonic() - began < 0.25  # ... and the page did not wait long for a newer
    checks.forget(addon.id)
    await checks.check([addon])  # nothing read before: the page says why
    assert (known := checks.known(addon)) is not None and known.error is not None
    assert known.error.startswith("busy")
    assert not len(pace._requests)  # nothing left waiting at the limit
    # Left alone after a rate limit: not asked either, at once.
    pace.block(30)
    with pytest.raises(addons.Busy, match="rate limited"):
        await addons.fetch_manifest(
            "https://a1.invalid/x", Reach.PUBLIC, resolver=nowhere, pace=pace_at
        )
    assert pace.sent == 1


@pytest.mark.anyio
async def test_pages_loading_together_read_a_manifest_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One manifest request for checks under way at the same time - also for a check
    that only gets to it once the other is done."""
    import anyio

    from shijhon.dashboard import addons

    addon = StoredSource(1, "A1", "https://a1.invalid", {}, True, 1, Reach.PUBLIC, None)
    calls: list[int] = []
    release = anyio.Event()

    async def counted(url: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        await release.wait()
        return addons.ManifestInfo("A1", "1.0", ())

    monkeypatch.setattr(addons, "fetch_manifest", counted)
    checks = addons.ManifestChecks()
    async with anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(checks.check, [addon])
        await anyio.sleep(0.02)
        assert len(calls) == 1  # the others wait for that one read
        release.set()
    assert len(calls) == 1 and (known := checks.known(addon)) is not None
    assert known.manifest is not None and known.manifest.version == "1.0"

    # A check that found the manifest due, and only runs once another has read it.
    checks.forget(addon.id)
    late = addons.ManifestChecks()
    started = anyio.Event()

    async def quick(url: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        started.set()
        return addons.ManifestInfo("A1", "1.0", ())

    monkeypatch.setattr(addons, "fetch_manifest", quick)
    real = late.known
    looks = 0

    def known_late(source: StoredSource) -> Any:
        nonlocal looks
        looks += 1
        if looks == 1:  # this check's first look: due - then another check reads it
            late.learn(source, addons.ManifestInfo("A1", "1.0", ()))
            return None
        return real(source)

    monkeypatch.setattr(late, "known", known_late)
    before = len(calls)
    await late.check([addon])
    assert len(calls) == before  # fresh by the time its turn came: not read again


def test_a_deeply_nested_manifest_is_not_json() -> None:
    import json

    nested = "[" * 100_000 + "]" * 100_000
    with pytest.raises(RecursionError):
        json.loads(nested)  # what fetch_manifest now turns into "manifest is not JSON"


def test_free_text_settings_with_secret_names_are_write_only() -> None:
    info = parse_manifest(
        {
            "settings": [
                {"key": "apiKey", "type": "string"},
                {"key": "token", "type": "str"},
                {"key": "password", "type": "input"},
                {"key": "region", "type": "string"},
                {"key": "server", "type": "url"},
                {"key": "webhookUrl", "type": "text"},
                {"key": "area", "type": "string", "secret": False},
                {"key": "sessionToken", "type": "text", "secret": False},
                {"key": "pinCode", "type": "text", "secret": False},
            ]
        }
    )
    assert [(d.key, d.kind) for d in info.declared] == [
        ("apiKey", "secret"),
        ("token", "secret"),
        ("password", "secret"),
        ("region", "secret"),  # any free text the manifest does not mark plain
        ("server", "secret"),  # add-on URLs often carry keys
        ("webhookUrl", "secret"),
        ("area", "text"),  # marked plain
        ("sessionToken", "secret"),  # ... which a key that looks like a secret never is
        ("pinCode", "secret"),
    ]


def test_a_secret_is_one_whatever_type_its_manifest_declares() -> None:
    """A key that looks like a secret is write-only also when its
    manifest declares it a number; a choice or an on/off is shown as one, and such a form
    never shows a stored value (``shown``)."""
    info = parse_manifest(
        {
            "settings": [
                {"key": "token", "type": "integer"},
                {"key": "apiKey", "type": "number", "default": 7},
                {"key": "userId", "type": "number"},
                {"key": "pin", "type": "int"},
                {"key": "account_id", "type": "float"},
                {"key": "ping", "type": "number"},  # not the word "pin"
                {"key": "codec", "type": "number"},  # not the word "code"
                {"key": "bitrate", "type": "number"},
                {"key": "session", "type": "select", "options": ["a", "b"]},
                {"key": "useToken", "type": "toggle"},
                {"key": "secretChoice", "secret": True, "options": ["a", "b"]},
                {"key": "level", "type": "number", "secret": True},
            ]
        }
    )
    assert [(d.key, d.kind) for d in info.declared] == [
        ("token", "secret"),
        ("apiKey", "secret"),
        ("userId", "secret"),
        ("pin", "secret"),
        ("account_id", "secret"),
        ("ping", "number"),
        ("codec", "number"),
        ("bitrate", "number"),
        ("session", "select"),
        ("useToken", "switch"),
        ("secretChoice", "secret"),
        ("level", "secret"),
    ]


def test_only_a_value_that_cannot_be_a_secret_is_shown() -> None:
    from shijhon.dashboard.addons import Declared, hidden_mark, mark, shown

    select = Declared("session", "Session", "select", None, (("a", "A"), ("7", "Seven")))
    switch = Declared("useToken", "Use it", "switch", None)
    number = Declared("bitrate", "Bitrate", "number", "320")
    text = Declared("area", "Area", "text", None)
    secret = Declared("token", "Token", "secret", None)
    for item in (select, switch, number, text):
        assert shown(item, None)  # nothing stored: the default
        assert not shown(item, ["a"]) and not shown(item, {"a": 1})
    assert shown(select, "a") and shown(select, 7)
    assert not shown(select, "tok-9f3a") and not shown(select, "A")  # a value, not a label
    assert shown(switch, True) and shown(switch, "false")
    assert not shown(switch, "tok-9f3a")
    # A number, a text: only one saved through the form's own visible field (its mark) -
    # not one from the configuration file, nor one stored under an earlier declaration.
    for value in (256, 19.5, "256,5", "4711"):
        assert not shown(number, value)
        assert shown(number, value, {"bitrate": mark(value)})
        assert not shown(number, value, {"bitrate": mark("another")})
        assert not shown(number, value, {"other": mark(value)})
    for value in ("tok-9f3a", True, "1e5x"):  # no number, however it got there
        assert not shown(number, value, {"bitrate": mark(value)})
    assert not shown(text, "eu") and not shown(text, 5)
    assert shown(text, "eu", {"area": mark("eu")}) and not shown(text, "us", {"area": mark("eu")})
    for value in (None, "tok-9f3a", 5):
        assert not shown(secret, value, {"token": mark(value)})
    # What was typed into a write-only field is never shown, whatever its manifest
    # declares it later - a choice that happens to list it, an on/off.
    assert shown(select, "7") and not shown(select, "7", {"session": hidden_mark("7")})
    assert shown(select, "7", {"session": hidden_mark("8")})  # (another value's mark)
    assert not shown(switch, "true", {"useToken": hidden_mark("true")})
    assert not shown(number, 256, {"bitrate": hidden_mark(256)})


def test_the_dashboard_can_be_turned_off(tmp_path: Path) -> None:
    on = ShijhonApp(_settings(tmp_path))
    off = ShijhonApp(_settings(tmp_path, server={"dashboard": False}))
    assert "/shijhon" in on.proxy.mounts
    assert "/shijhon" not in off.proxy.mounts  # the path is forwarded to Navidrome


def test_trusted_proxies_must_be_addresses() -> None:
    with pytest.raises(ValueError):
        load_settings(None, server={"trusted_proxies": ["not-a-network"]})
    with pytest.raises(ValueError):
        load_settings(None, delivery={"pin_ttl_seconds": float("inf")})


def test_the_environment_wins_over_a_saved_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIJHON_CATALOG__KIND", "sample")
    monkeypatch.setenv("SHIJHON_CATALOG__TOKEN", "from-env")
    configured = load_settings(None)
    saved = {
        "catalog": {
            "token": Saved("from-dashboard", 0.0, "t"),
            ADAPTER: Saved("sample", 0.0, "t"),
        }
    }
    applying, _ = effective(configured, saved, locked=locked_keys(configured))
    assert applying.catalog.token is not None
    assert applying.catalog.token.get_secret_value() == "from-env"
    unlocked, _ = effective(configured, saved, locked={})  # without the environment's lock
    assert unlocked.catalog.token is not None
    assert unlocked.catalog.token.get_secret_value() == "from-dashboard"


@pytest.mark.anyio
async def test_startup_keeps_the_configuration_when_saved_values_no_longer_work(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, delivery={"budget_seconds": 12})
    store = await Store.open(settings.database_path)
    saved = SavedSettings(store)
    # Valid when saved; the file's budget was raised later.
    await saved.put("delivery", {"max_wait_seconds": 10.0, "cooldown_seconds": 5.0}, "t")
    # A token file that was removed since: the catalog could not be built.
    gone = {"kind": "sample", "token_file": str(tmp_path / "gone"), ADAPTER: "sample"}
    await saved.put("catalog", gone, "t")
    await store.close()
    app = ShijhonApp(settings)
    await app.startup()
    try:
        assert app.services is not None
        assert app.services.deliverer.settings.max_wait_seconds == 30  # the configuration's
        assert app.services.deliverer.settings.cooldown_seconds == 30  # the whole section
        assert app.services.catalog is None and app.settings.catalog.kind == "none"
        problems = app.dashboard.problems
        assert "max_wait_seconds" in problems["delivery"]
        assert "token_file" in problems["catalog"]
        assert "saved delivery settings are not in use" in caplog.text
    finally:
        await app.shutdown()


@pytest.mark.parametrize(
    ("typed", "state"),
    [("x", "saved-over-config"), ("", "config-only")],
)
def test_a_removed_secret_can_come_back_from_the_configuration(typed: str, state: str) -> None:
    import anyio

    configured = load_settings(None, catalog={"kind": "sample", "token": "from-file"})
    saved = (
        {"catalog": {"token": Saved("mine", 0.0, "t"), ADAPTER: Saved("sample", 0.0, "t")}}
        if state == "saved-over-config"
        else {}
    )

    async def run() -> sections.Written:
        submitted = await sections.submit(
            "catalog", configured, saved, {"token-clear": "true"}, addon_names=[], locked={}
        )
        return sections.writes("catalog", configured, saved, submitted, locked={})

    written = anyio.run(run)
    if state == "saved-over-config":
        # The file's token applies again (and nothing of the adapter's is saved).
        assert written.writes == {"token": REMOVE, ADAPTER: REMOVE}
    else:
        # ... hides the file's token (saved for this catalog's adapter)
        assert written.writes == {"token": None, ADAPTER: "sample"}


def test_the_token_source_is_one_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "shijhon.toml"
    config.write_text('[catalog]\nkind = "sample"\ntoken = "from-the-file"\n')
    assert load_settings(config).catalog.secret_token() == "from-the-file"
    monkeypatch.setenv("SHIJHON_CATALOG__TOKEN_URL", "https://tokens.invalid/issue")
    configured = load_settings(config)
    assert configured.catalog.token is None  # the environment's choice replaces the file's
    assert configured.catalog.token_url is not None
    overridden = load_settings(config, catalog={"token": "given"})  # overrides still win
    assert overridden.catalog.token is not None
    assert overridden.catalog.token.get_secret_value() == "given"


def test_variables_are_named_as_they_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIJHON_CATALOG__HEADERS__X_ONE", "1")
    monkeypatch.setenv("SHIJHON_ADDONS", '[{"name": "E", "base_url": "https://e.invalid/k"}]')
    configured = load_settings(None)
    headers = configured.from_environment["catalog"]["headers"]
    assert headers == "SHIJHON_CATALOG__HEADERS__X_ONE"
    assert configured.from_environment["addons"] == {"list": "SHIJHON_ADDONS"}


@pytest.mark.anyio
async def test_an_addon_list_from_the_environment_always_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "SHIJHON_ADDONS", '[{"name": "FromEnv", "base_url": "https://env.example.invalid/k"}]'
    )
    settings = _settings(tmp_path)
    store = await Store.open(settings.database_path)
    await SavedSettings(store).take_addons("tester")  # the dashboard had changed the list
    await store.close()
    app = ShijhonApp(settings)
    await app.startup()
    try:
        assert app.services is not None
        assert [s.name for s in await app.services.sources.enabled()] == ["FromEnv"]
    finally:
        await app.shutdown()


def test_older_times_carry_their_day() -> None:
    import time as clock

    from shijhon.dashboard.app import when

    now = clock.time()
    assert re.fullmatch(r"\d\d:\d\d", when(now))
    assert re.fullmatch(r"\d{1,2} \w+, \d\d:\d\d", when(now - 3 * 86400))


@pytest.mark.parametrize(
    ("text", "at"),
    [
        ("2025-03-14T08:05:12.123456789Z", 1741939512.123456),  # Go's nanoseconds
        ("2025-03-14T03:05:12-05:00", 1741939512.0),
        ("0001-01-01T00:00:00Z", None),  # Go's zero time: no scan yet
        ("2025-03-14T08:05:12", None),  # no zone: which time it is is unknown
        ("yesterday", None),
        (None, None),
        (17, None),
    ],
)
def test_the_last_scan_is_read_as_a_time(text: object, at: float | None) -> None:
    from shijhon.dashboard.status import scan_time

    assert scan_time(text) == (pytest.approx(at, abs=1e-6) if at is not None else None)


def test_templates_never_read_a_dict_method_as_a_key() -> None:
    """Jinja's ``row.clear`` finds ``dict.clear`` before the row's "clear" key (a page
    showed "<built-in method clear …>"): no template reads a name dicts have as an
    attribute, unless it calls it (``described.append(…)``)."""
    from jinja2 import Environment, PackageLoader, nodes

    env = Environment(loader=PackageLoader("shijhon.dashboard", "templates"), autoescape=True)
    methods = {name for name in dir(dict) if not name.startswith("_")}
    found = []
    templates = env.list_templates(extensions=["html"])
    assert "_macros.html" in templates and "library.html" in templates
    for name in templates:
        tree = env.parse(env.loader.get_source(env, name)[0])  # type: ignore[union-attr]
        called = {id(call.node) for call in tree.find_all(nodes.Call)}
        for node in tree.find_all(nodes.Getattr):
            if node.attr in methods and id(node) not in called:
                found.append(f"{name}:{node.lineno} .{node.attr}")
    assert not found


# --- the Library page -------------------------------------------------------------------


def test_review_reasons_are_explained() -> None:
    from shijhon.dashboard.library import explain

    assert explain("several plausible editions") == (
        "Several releases match",
        "More than one edition fits the owned songs.",
        False,
    )
    part = explain("part of Mira Solane - Glass Orchard (album a1b2c3): filled there")
    assert part == ("Part of another album", "Mira Solane - Glass Orchard: filled there.", True)
    assert explain("something new") == ("Something new", "", False)


def test_a_share_is_a_percentage() -> None:
    share = _fields("fill")["auto_min_share"]
    assert (share.kind, share.unit, share.low, share.high) == ("decimal", "%", 0, 100)
    assert share.parse("25") == 0.25 and share.parse("12,5") == 0.125
    assert share.show(0.29) == "29" and share.show(0.125) == "12.5"
    assert share.describe(0.25) == "25 %"
    for typed in ("150", "abc", "-1"):
        with pytest.raises(Invalid, match=r"^Enter a percentage from 0 to 100, e\.g\. 25\.$"):
            share.parse(typed)


@pytest.mark.anyio
async def test_the_pass_mode_applies_at_once_only_between_dry_run_and_on() -> None:
    from types import SimpleNamespace

    from shijhon.config import FillSettings
    from shijhon.dashboard import live

    passing = SimpleNamespace(mode="dry_run")
    services: Any = SimpleNamespace(library_pass=passing)
    on = FillSettings(library_pass="on")
    assert await live.apply(services, "fill", ["library_pass"], on) == ["library_pass"]
    assert passing.mode == "on"
    off = FillSettings(library_pass="off")
    assert await live.apply(services, "fill", ["library_pass"], off) == []  # a restart...
    assert passing.mode == "dry_run"  # ...but it stops filling at once
    assert await live.apply(services, "fill", ["library_pass"], on) == ["library_pass"]
    assert passing.mode == "on"  # changed back before the restart
    passing.mode = "dry_run"
    assert await live.apply(services, "fill", ["library_pass"], off) == []
    assert passing.mode == "dry_run"  # a dry run fills nothing: left as it is
    passing.mode = "off"  # a pass that started off is not running
    assert await live.apply(services, "fill", ["library_pass"], on) == []
    assert passing.mode == "off"
    # No library pass at all (no catalog, or filling off): after a restart, quietly.
    nothing: Any = SimpleNamespace(library_pass=None)
    marked: list[str] = []
    assert await live.apply(nothing, "fill", ["library_pass"], off, marked) == []
    assert await live.apply(nothing, "fill", ["library_pass"], on, marked) == []
    assert marked == []
    # The interim is reported.
    passing.mode = "on"
    assert await live.apply(services, "fill", ["library_pass"], off, marked) == []
    assert marked == ["library_pass"] and passing.mode == "dry_run"


@pytest.mark.anyio
async def test_commands_see_the_values_saved_in_the_dashboard(tmp_path: Path) -> None:
    from shijhon.dashboard.saved import effective_from_database

    database = tmp_path / "shijhon.sqlite3"
    configured = load_settings(None, fill={"auto_min_songs": 3})
    assert effective_from_database(configured, database).fill.auto_min_songs == 3  # none yet
    store = await Store.open(database)
    await SavedSettings(store).put("fill", {"auto_min_songs": 5}, "tester")
    await store.close()
    assert effective_from_database(configured, database).fill.auto_min_songs == 5


def test_background_actions_are_shown_for_a_while() -> None:
    from shijhon.dashboard.library import Jobs

    now = [1000.0]
    jobs = Jobs(clock=lambda: now[0])
    job = jobs.start("a1", "Artist - Album", "fill")
    assert jobs.running("a1") is job and jobs.recent() == []
    jobs.finish(job, ok=True, message="done")
    assert jobs.running("a1") is None and jobs.recent() == [job]
    now[0] += 901
    assert jobs.recent() == []


@pytest.mark.anyio
async def test_candidate_releases_are_described_within_a_few_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import anyio

    from shijhon.dashboard import library

    async def album(item: str) -> Any:
        if item == "slow":
            await anyio.sleep(3600)
        return SimpleNamespace(
            title="Deluxe edition",
            year=2020,
            tracks=(1,) * 18,
            track_count=18,
            clean=False,
            explicit=False,
        )

    monkeypatch.setattr(library, "DESCRIBE_SECONDS", 0.3)
    catalog = SimpleNamespace(key="demo", album=album)
    describer = library.Describer()
    await describer.describe(catalog, ["demo:1", "demo:slow", "other:2"])
    assert describer.label("demo:1") == "Deluxe edition, 2020, 18 songs"
    assert describer.label("demo:slow") is None and describer.label("other:2") is None


def test_parts_are_found_by_their_reason_whatever_the_names() -> None:
    from shijhon.dashboard.library import explain, is_part

    reason = "part of Line\nBreak - Title (album z9): complete there"
    assert is_part(reason) and explain(reason)[2]
    assert explain(reason)[1] == "Line\nBreak - Title: complete there."


def test_actions_are_claimed_and_shown_once_per_viewer() -> None:
    from shijhon.dashboard.library import Jobs

    jobs = Jobs()
    job = jobs.claim("a1", "Album", "fill")
    assert job is not None and jobs.claim("a1", "Album", "keep") is None
    jobs.finish(job, ok=True, message="done")
    assert jobs.recent("viewer-1") == [job] and jobs.recent("viewer-1") == []
    assert jobs.recent("viewer-2") == [job]
    kept = jobs.claim("a2", "Other", "keep")
    assert kept is not None
    jobs.drop(kept)
    assert jobs.running("a2") is None and jobs.recent("viewer-3") == [job]


@pytest.mark.anyio
async def test_a_release_not_described_is_asked_again_only_later() -> None:
    from types import SimpleNamespace

    from shijhon.catalog.base import CatalogError
    from shijhon.dashboard import library

    asked: list[str] = []

    async def album(item: str) -> Any:
        asked.append(item)
        raise CatalogError("not_found", "not in the catalog")

    now = [0.0]
    describer = library.Describer(clock=lambda: now[0])
    catalog = SimpleNamespace(key="demo", album=album)
    await describer.describe(catalog, ["demo:1"])
    await describer.describe(catalog, ["demo:1"])
    assert asked == ["1"] and describer.label("demo:1") is None
    now[0] += 901
    await describer.describe(catalog, ["demo:1"])
    assert asked == ["1", "1"]


@pytest.mark.anyio
async def test_the_meter_counts_only_albums_in_the_library(tmp_path: Path) -> None:
    from shijhon.dashboard import library

    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        for album_id, outcome, planned in (
            ("a1", "filled", 1),
            ("a2", "review", 0),
            ("gone", "none", 0),
            ("a3", "complete", 0),
        ):
            await store.execute(
                "INSERT INTO album_matches (album_id, scope, outcome, planned, checked_at)"
                " VALUES (?, 'demo.us', ?, ?, 0)",
                [album_id, outcome, planned],
            )
        await store.execute(
            "INSERT INTO releases (ref, folder, album_id, owned_album_id, title, artist,"
            " album_tags, data, created_at) VALUES ('demo:9', 'f', 'a4', 'a4', 't', 'a', '{}',"
            " '{}', 0)"
        )
        found = await library.counts(store, "demo.us", {"a1", "a2", "a3", "a4"})
        assert (found.would_fill, found.review, found.complete, found.none, found.filled) == (
            1,
            1,
            1,
            0,
            1,
        )
        assert found.checked == 4
    finally:
        await store.close()


@pytest.mark.anyio
async def test_albums_without_a_match_count_as_checked_not_matched(tmp_path: Path) -> None:
    from shijhon.dashboard import library

    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        for album_id in ("b1", "b2", "b3", "b4"):
            await store.execute(
                "INSERT INTO album_matches (album_id, scope, outcome, planned, checked_at)"
                " VALUES (?, 'demo.us', 'none', 0, 0)",
                [album_id],
            )
        found = await library.counts(store, "demo.us", {"b1", "b2", "b3", "b4"})
        assert (found.checked, found.none) == (4, 4)
        assert found.would_fill + found.filled + found.deferred + found.complete == 0
    finally:
        await store.close()


def test_a_database_that_cannot_be_read_is_an_error(tmp_path: Path) -> None:
    import sqlite3

    from shijhon.dashboard.saved import effective_from_database

    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not a database at all" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        effective_from_database(load_settings(None), broken)


# --- secrets and a new host, saved types, counts --------------------------------------------


def test_the_token_services_headers_stay_with_its_host() -> None:
    """Headers the configuration sets for its token service (often a key) are never sent to
    a token service saved in the dashboard on another host."""
    from shijhon.dashboard.saved import withheld

    configured = load_settings(
        None,
        catalog={
            "kind": "sample",
            "token_url": "https://tokens.example.invalid/issue?key=1",
            "token_headers": {"x-api-key": "header-secret"},
        },
    )

    def applying(url: str) -> Settings:
        saved = {"catalog": {"token_url": Saved(url, 0.0, "t"), ADAPTER: Saved("sample", 0.0, "t")}}
        return effective(configured, saved, locked={})[0]

    same = applying("https://TOKENS.example.invalid:443/other?key=2")  # the same host
    assert same.catalog.token_headers.keys() == {"x-api-key"}
    for url in (
        "https://elsewhere.example.invalid/issue",
        "http://tokens.example.invalid/issue",  # another scheme
        "https://tokens.example.invalid:8443/issue",  # another port
        "not a url",
    ):
        moved = applying(url)
        assert moved.catalog.token_headers == {}, url
        assert withheld(configured.catalog, moved.catalog)
    # A token of its own makes the token service unused: nothing to say (still not sent).
    tokened = effective(
        configured,
        {"catalog": {"token_url": Saved("https://elsewhere.example.invalid/", 0.0, "t"),
                       "token": Saved("a-token", 0.0, "t"), ADAPTER: Saved("sample", 0.0, "t")}},
        locked={},
    )[0]  # fmt: skip
    assert tokened.catalog.token_headers == {}
    assert not withheld(configured.catalog, tokened.catalog)
    assert configured.catalog.token_headers  # the configuration itself untouched
    # Without a token service of its own in the configuration: never sent to one saved here.
    bare = load_settings(
        None, catalog={"kind": "sample", "token_headers": {"x-api-key": "header-secret"}}
    )
    saved = {
        "catalog": {
            "token_url": Saved("https://tokens.example.invalid/", 0.0, "t"),
            ADAPTER: Saved("sample", 0.0, "t"),
        }
    }
    assert effective(bare, saved, locked={})[0].catalog.token_headers == {}


@pytest.mark.anyio
async def test_a_token_service_on_another_host_gets_no_headers_at_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(
        tmp_path,
        catalog={
            "kind": "sample",
            "token_url": "https://tokens.example.invalid/issue",
            "token_headers": {"x-api-key": "header-secret"},
        },
    )
    store = await Store.open(settings.database_path)
    await SavedSettings(store).put(
        "catalog",
        {"token_url": SecretStr("https://other.example.invalid/issue"), ADAPTER: "sample"},
        "tester",
    )
    await store.close()
    app = ShijhonApp(settings)
    with caplog.at_level(logging.WARNING, logger="shijhon"):
        await app.startup()
    try:
        assert app.services is not None and app.services.catalog is not None
        # What the adapter was built with: none of the configuration's headers.
        assert app.services.catalog.inner.token_headers == {}  # type: ignore[attr-defined]
        assert "not sent: the configuration's token service headers" in caplog.text
        assert "header-secret" not in caplog.text
    finally:
        await app.shutdown()


@pytest.mark.anyio
async def test_a_changed_file_list_the_dashboard_keeps_from_is_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Once the dashboard keeps the add-on list, the file's is not applied - said as a
    warning at startup when the file differs (its edits would do nothing)."""
    addons = [{"name": "One", "base_url": "https://one.example.invalid/cfg"}]
    app = ShijhonApp(_settings(tmp_path, addons=addons))
    await app.startup()
    assert app.dashboard.saved is not None
    await app.dashboard.saved.take_addons("tester")
    await app.shutdown()
    edited = [{**addons[0], "budget_seconds": 7}]  # the owner edits the file
    app = ShijhonApp(_settings(tmp_path, addons=edited))
    with caplog.at_level(logging.INFO, logger="shijhon"):
        await app.startup()
    try:
        warned = [r for r in caplog.records if "[[addons]] are not applied" in r.message]
        assert warned and warned[0].levelno == logging.WARNING
        assert "differs in: One" in warned[0].message
        assert app.services is not None
        [one] = await app.services.sources.enabled()
        assert one.budget is None  # not applied
    finally:
        await app.shutdown()


def test_an_addons_setting_is_saved_only_when_changed_with_its_kind() -> None:
    from shijhon.dashboard.addons import KEPT, Declared, mark
    from shijhon.dashboard.app import _addon_value, _Unfit

    number = Declared("bitrate", "Bitrate", "number", "320")
    switch = Declared("lossless", "Lossless", "switch", None)
    select = Declared("codec", "Codec", "select", None, (("flac", "FLAC"), ("opus", "Opus")))
    text = Declared("region", "Region", "text", "eu")
    # (A number or a text is on the form only when saved through its own field: its mark.)
    saved = {"bitrate": mark(256), "region": mark("eu")}
    as_text = {"bitrate": mark("256")}
    assert _addon_value(number, 256, "256", saved) == (False, 256)
    assert _addon_value(number, "256", "256,0", as_text) == (False, 256)  # the same number
    assert _addon_value(number, None, "320")[0] is False  # the default, as shown
    assert _addon_value(number, "256", "256", as_text) == (False, "256")  # as text: kept
    assert _addon_value(number, 256, "192", saved) == (True, 192)
    assert _addon_value(number, 256, "19.5", saved) == (True, 19.5)
    assert _addon_value(number, 256, "", saved) == (True, None)  # the add-on's default
    with pytest.raises(_Unfit):
        _addon_value(number, 256, "fast", saved)
    assert _addon_value(switch, None, "false") == (False, False)  # no default: not frozen
    assert _addon_value(switch, True, "true") == (False, True)
    assert _addon_value(switch, True, "false") == (True, False)
    assert _addon_value(select, None, "") == (False, None)
    assert _addon_value(select, "flac", "opus") == (True, "opus")
    assert _addon_value(select, "flac", "") == (True, None)  # back to the add-on's default
    with pytest.raises(_Unfit):
        _addon_value(select, "flac", "mp3")
    # A stored value that is none of the options is never shown: kept by the form's entry
    # for it, replaced by a choice, removed by "the add-on's default".
    assert _addon_value(select, "wav", KEPT) == (False, "wav")
    assert _addon_value(select, "wav", "opus") == (True, "opus")
    assert _addon_value(select, "wav", "") == (True, None)
    with pytest.raises(_Unfit):
        _addon_value(select, "wav", "wav")
    with pytest.raises(_Unfit):
        _addon_value(select, "flac", KEPT)  # nothing was kept there
    assert _addon_value(switch, "wav", "false") == (False, False)  # (nothing shown, kept)
    assert _addon_value(text, None, "eu")[0] is False
    assert _addon_value(text, "eu", "us", saved) == (True, "us")
    assert _addon_value(text, "eu", "eu", saved) == (False, "eu")


def test_only_settings_a_manifest_declares_plain_go_to_a_new_host() -> None:
    from shijhon.dashboard.addons import plain_keys
    from shijhon.delivery.pacing import origin

    old = parse_manifest(
        {
            "settings": [
                {"key": "apiKey", "type": "string", "secret": False},
                {"key": "tier", "secret": False},
                {"key": "codec", "options": ["flac"]},
                {"key": "note"},
            ]
        }
    )
    new = parse_manifest({"settings": [{"key": "tier", "type": "password"}, {"key": "hq"}]})
    # apiKey: named like a key; note: free text not marked plain
    assert plain_keys(old) == {"tier", "codec"}
    assert plain_keys(old, new) == {"codec"}  # tier: a secret for the new one
    assert plain_keys(None, new) == set()  # the old manifest unread: nothing is plain
    access = parse_manifest({"settings": [{"key": "access", "type": "password"}]})
    loose = parse_manifest({"settings": [{"key": "access", "type": "text", "secret": False}]})
    assert plain_keys(access, loose) == set()  # the new one cannot make a secret plain
    assert origin("https://A.example.invalid/x?k=1") == ("https", "a.example.invalid", 443)
    assert origin("http://a.example.invalid:8080/") == ("http", "a.example.invalid", 8080)
    assert origin("") is None and origin("no host") is None


@pytest.mark.anyio
async def test_deferred_albums_the_policy_allows_would_be_filled(tmp_path: Path) -> None:
    """Albums deferred under an earlier policy that the current one
    allows are filled as soon as automatic fills run - counted as "would fill"."""
    from shijhon.dashboard import library
    from shijhon.fill.fills import FillPolicy, Fills

    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        for album_id, outcome, planned, owned, tracks, cleaned in (
            ("allowed", "deferred", 0, 3, 12, None),
            ("share", "deferred", 0, 2, 8, None),  # a quarter
            ("below", "deferred", 0, 1, 12, None),
            ("cleaned", "deferred", 0, 5, 12, 1.0),  # its fill taken out: on its next use
            ("plan", "filled", 1, 4, 12, None),  # a dry run's plan
            ("elsewhere", "deferred", 0, 6, 12, None),  # filled under another catalog
        ):
            await store.execute(
                "INSERT INTO album_matches (album_id, scope, outcome, planned, checked_at,"
                " owned_songs, release_tracks, cleaned_at)"
                " VALUES (?, 'demo.us', ?, ?, 0, ?, ?, ?)",
                [album_id, outcome, planned, owned, tracks, cleaned],
            )
        await store.execute(
            "INSERT INTO releases (ref, folder, album_id, owned_album_id, title, artist,"
            " album_tags, data, created_at) VALUES ('other:1', 'f', 'elsewhere', 'elsewhere',"
            " 't', 'a', '{}', '{}', 0)"
        )
        policy = FillPolicy(3, 0.25)
        found = await library.counts(store, "demo.us", None, policy)
        assert (found.would_fill, found.deferred, found.filled) == (3, 2, 1)
        fills = Fills(store, None, None, None, scope="demo.us", policy=policy)  # type: ignore[arg-type]
        assert await fills.would_fill() == 3
        assert await fills.would_fill({"allowed", "share", "elsewhere"}) == 2
        # A stricter policy: the plan too is filled on first use instead.
        stricter = await library.counts(store, "demo.us", None, FillPolicy(10, 0.9))
        assert (stricter.would_fill, stricter.deferred) == (0, 5)
        unknown = await library.counts(store, "demo.us", None)  # no policy: plans only
        assert (unknown.would_fill, unknown.deferred) == (1, 4)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_a_dry_run_counts_the_deferred_albums_it_would_fill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from shijhon.fill.library_pass import LibraryPass

    class Fills:
        auto_fill = True

        async def wait_idle(self) -> None:
            return None

        async def due(self, album_id: str, **kwargs: Any) -> bool:
            return False  # a dry run does not look at deferred albums again

        async def would_fill(self, albums: set[str] | None = None) -> int:
            assert albums == {"a1"}  # those in the library
            return 7

    class Navidrome:
        async def album_counts(self) -> list[tuple[str, int, int | None]]:
            return [("a1", 3, None)]

    passing = LibraryPass(Fills(), Navidrome(), None, mode="dry_run")  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO, logger="shijhon"):
        await passing.run_once()
    assert "switched on, it would fill 7 album(s) in all" in caplog.text
    caplog.clear()
    passing.mode = "on"
    with caplog.at_level(logging.INFO, logger="shijhon"):
        await passing.run_once()
    assert "in all" not in caplog.text
