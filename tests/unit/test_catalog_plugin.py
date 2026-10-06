"""Catalog adapters as plugins: how an adapter is found, how its declared
settings become part of ``[catalog]`` (the file, the environment, the dashboard's saved
values and locks), and that one adapter's settings never reach another."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic import BaseModel, Field, SecretStr, ValidationError

from shijhon.catalog import plugin
from shijhon.catalog.plugin import Adapter, AdapterError, Context, Problem, Words
from shijhon.catalog.setup import build_catalog
from shijhon.config import CatalogSettings, catalog_settings, load_settings
from shijhon.dashboard import sections
from shijhon.dashboard.saved import (
    ADAPTER,
    BOUND,
    REMOVE,
    Saved,
    SavedSettings,
    effective,
    list_saved,
    locked_keys,
    out_of_place,
    place,
    withheld,
)
from shijhon.dashboard.sections import SHOWN_KIND
from shijhon.store import Store
from tests.harness.sample_adapter import KIND as SAMPLE_KIND
from tests.harness.sample_adapter import SAMPLE


class Built:
    """What an adapter built: the settings it was given."""

    key = "made"

    def __init__(self, settings: Any, context: Context) -> None:
        self.settings = settings
        self.context = context
        self.region = getattr(settings, "region", "")


class OneSettings(BaseModel):
    region: str = "xx"
    token: SecretStr | None = None
    token_file: Path | None = None
    service: SecretStr | None = None
    service_headers: dict[str, SecretStr] = Field(default_factory=dict)
    rate: float = Field(default=5.0, gt=0)


def one_problem(settings: Any) -> Problem | None:
    if settings.token is None and settings.token_file is None and settings.service is None:
        return Problem("token", "Set a token.", "is needed.")
    return None


ONE = Adapter(
    label="Catalog One",
    build=Built,
    settings=OneSettings,
    words={
        "region": Words("Region", "Two letters.", short=True, pattern=r"[a-z]{2}"),
        "token": Words("Token"),
        "token_file": Words("Token file"),
        "service": Words("Token service"),
        "service_headers": Words("Token service headers", group="advanced"),
    },
    one_choice=(("token", "token_file", "service"),),
    bound={"service_headers": "service"},
    problem=one_problem,
    notice="Catalog One is made up.",
)


class TwoSettings(BaseModel):
    token: SecretStr | None = None  # the same name as One's


TWO = Adapter(label="Catalog Two", build=Built, settings=TwoSettings)
BARE = Adapter(label="Bare", build=Built)  # no settings of its own


@pytest.fixture(autouse=True)
def adapters(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.upper().startswith("SHIJHON_"):
            monkeypatch.delenv(name)
    plugin.unregister(SAMPLE_KIND)
    plugin.register("one", ONE)
    plugin.register("two", TWO)
    plugin.register("bare", BARE)
    yield
    for name in ("one", "two", "bare"):
        plugin.unregister(name)
    plugin.register(SAMPLE_KIND, SAMPLE)


def test_an_adapter_is_found_by_its_kind_and_others_are_refused() -> None:
    assert plugin.adapter("one") is ONE and "one" in plugin.installed()
    with pytest.raises(AdapterError, match=r"no catalog adapter named 'elsewhere'.*one"):
        plugin.adapter("elsewhere")
    with pytest.raises(ValidationError) as refused:
        load_settings(None, catalog={"kind": "elsewhere", "token": "tok-SECRET"})
    text = str(refused.value)
    assert "no catalog adapter named 'elsewhere' is installed" in text
    assert "SECRET" not in text
    for name in ("none", "Upper", "9lives", ""):
        with pytest.raises(AdapterError, match="name"):
            plugin.register(name, ONE)


def test_installed_adapters_come_from_the_entry_point_group() -> None:
    """The group is ``shijhon.catalogs``: whatever the environment's packages register
    there is installed (the tests' made-up catalog among them)."""
    from importlib.metadata import entry_points

    names = {entry.name for entry in entry_points(group=plugin.GROUP)}
    assert names <= set(plugin.installed())
    plugin.unregister("one")
    assert "one" not in plugin.installed()  # only what is registered or installed


def test_the_made_up_catalog_is_not_shijhon_s_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shijhon's own package registers one kind, the catalog of an add-on, and no
    catalog with data of its own: the made-up one is a package of its own, in the
    development environment only. Where it is not installed, ``kind = "demo"`` is refused
    like any kind nobody installed."""
    from importlib.metadata import distribution, entry_points

    own = {e.name for e in distribution("shijhon").entry_points if e.group == plugin.GROUP}
    assert own == {"addon"}
    [demo] = [e for e in entry_points(group=plugin.GROUP) if e.name == "demo"]
    assert demo.dist is not None and demo.dist.name == "shijhon-demo-catalog"
    monkeypatch.setattr(plugin, "entry_points", lambda group: [])
    monkeypatch.setattr(plugin, "_loaded", None)
    monkeypatch.setattr(plugin, "_broken", {})
    try:
        assert "demo" not in plugin.installed()
        with pytest.raises(ValidationError) as refused:
            load_settings(None, catalog={"kind": "demo"})
        assert "no catalog adapter named 'demo' is installed" in str(refused.value)
    finally:
        monkeypatch.undo()
        plugin.unregister("never-registered")  # the settings made for the adapters: anew


def test_a_declaration_is_checked() -> None:
    """What an adapter declares is checked when it is loaded: one that does not fit is not
    installed (and says why), never found later by a page or a command."""

    class Clash(BaseModel):
        twins: str = "x"  # one of Shijhon's own [catalog] settings

    with pytest.raises(AdapterError, match="declares settings Shijhon has itself: twins"):
        plugin.register("bad", Adapter(label="Clash", build=Built, settings=Clash))
    with pytest.raises(AdapterError, match="does not declare: nowhere"):
        plugin.register("bad", Adapter(label="B", build=Built, words={"nowhere": Words("N")}))

    class Required(BaseModel):
        token: SecretStr = SecretStr("x")

    with pytest.raises(AdapterError, match="must be optional"):
        plugin.register(
            "bad", Adapter(label="B", build=Built, settings=Required, one_choice=(("token",),))
        )

    class Needed(BaseModel):
        token: SecretStr

    with pytest.raises(AdapterError, match="needs a default"):
        plugin.register("bad", Adapter(label="B", build=Built, settings=Needed))

    class Listed(BaseModel):
        languages: list[str] = ["en"]  # a type the page could not save back

    with pytest.raises(AdapterError, match="languages has a type"):
        plugin.register("bad", Adapter(label="B", build=Built, settings=Listed))

    class Loose(BaseModel):
        service: SecretStr | None = None
        rate: float = 1.0

    with pytest.raises(AdapterError, match="bound to no address"):
        plugin.register(
            "bad", Adapter(label="B", build=Built, settings=Loose, bound={"service": "rate"})
        )
    # An address is not itself bound (what is bound to it would
    # follow whatever address took its place when it is held back).
    with pytest.raises(AdapterError, match="which is itself bound to an address"):
        plugin.register(
            "bad",
            Adapter(
                label="B",
                build=Built,
                settings=OneSettings,
                bound={"service_headers": "service", "service": "region"},
            ),
        )
    # A choice of the add-ons is offered for a text that names one, and nothing else.
    for words in (Words("Rate", choices="sources"), Words("Service", choices="elsewhere")):
        key = "rate" if words.label == "Rate" else "service"
        with pytest.raises(AdapterError, match="offers choices the page does not have"):
            plugin.register(
                "bad", Adapter(label="B", build=Built, settings=Loose, words={key: words})
            )
    with pytest.raises(AdapterError, match=r"not a shijhon\.catalog\.plugin\.Adapter"):
        plugin.register("bad", object())  # type: ignore[arg-type]
    assert "bad" not in plugin.installed()
    assert plugin.validate("fine", ONE) is ONE


def test_a_broken_or_doubled_entry_point_is_left_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An installed adapter that cannot be loaded is not installed, and says why - without
    taking the others down; a name two packages register is neither's."""

    class Entry:
        def __init__(self, name: str, value: Any) -> None:
            self.name, self.value = name, value

        def load(self) -> Any:
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    entries = [
        Entry("good", TWO),
        Entry("gone", ImportError("no module named SECRET-PATH")),
        Entry("wrong", object()),
        Entry("Bad Name", TWO),
        Entry("twice", TWO),
        Entry("twice", BARE),
    ]
    monkeypatch.setattr(plugin, "entry_points", lambda group: entries)
    monkeypatch.setattr(plugin, "_loaded", None)
    monkeypatch.setattr(plugin, "_broken", {})
    with caplog.at_level("ERROR", logger="shijhon.catalog.plugin"):
        installed = plugin.installed()
    assert "good" in installed and not {"gone", "wrong", "Bad Name", "twice"} & set(installed)
    broken = dict(plugin.broken())
    assert broken["gone"] == "ImportError" and "SECRET-PATH" not in caplog.text
    assert "two installed packages" in broken["twice"]
    assert "not a shijhon.catalog.plugin.Adapter" in broken["wrong"]
    with pytest.raises(AdapterError, match="'gone' could not be loaded: ImportError"):
        plugin.adapter("gone")
    with pytest.raises(ValidationError, match="could not be loaded"):
        load_settings(None, catalog={"kind": "twice"})


def test_the_adapter_s_settings_are_part_of_the_catalog_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "shijhon.toml"
    config.write_text(
        '[catalog]\nkind = "one"\nregion = "gb"\ntoken = "from-the-file"\nrate = 2\n'
        'twins = "clean"\n[catalog.service_headers]\nx-key = "header-SECRET"\n'
    )
    settings = load_settings(config)
    catalog = settings.catalog
    assert isinstance(catalog, CatalogSettings) and isinstance(catalog, OneSettings)
    assert (catalog.kind, catalog.region, catalog.rate) == ("one", "gb", 2)
    assert catalog.twins == "clean" and catalog.cache_seconds == 3600  # Shijhon's own
    assert catalog.token.get_secret_value() == "from-the-file"  # type: ignore[union-attr]
    assert "SECRET" not in repr(settings) and "from-the-file" not in repr(settings)
    with pytest.raises(ValidationError):  # the adapter's limits are checked
        load_settings(None, catalog={"kind": "one", "rate": 0})
    # The environment: the adapter's settings like any other, and locked in the dashboard.
    monkeypatch.setenv("SHIJHON_CATALOG__REGION", "de")
    monkeypatch.setenv("SHIJHON_CATALOG__SERVICE_HEADERS__X_OTHER", "v")
    settings = load_settings(config)
    assert settings.catalog.region == "de"  # type: ignore[attr-defined]
    assert set(settings.catalog.service_headers) == {"x-key", "x_other"}  # type: ignore[attr-defined]
    assert set(locked_keys(settings)["catalog"]) == {"region", "service_headers"}
    # Without the kind, the adapter's settings are not Shijhon's: ignored, as unknown keys are.
    config.write_text('[catalog]\ntoken = "from-the-file"\n')
    monkeypatch.delenv("SHIJHON_CATALOG__REGION")
    assert not hasattr(load_settings(config).catalog, "token")
    # An adapter without settings of its own.
    assert catalog_settings("bare") is CatalogSettings
    assert load_settings(None, catalog={"kind": "bare"}).catalog.kind == "bare"


def test_one_choice_the_environment_replaces_the_file_s_and_locks_them_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "shijhon.toml"
    config.write_text('[catalog]\nkind = "one"\ntoken = "from-the-file"\n')
    monkeypatch.setenv("SHIJHON_CATALOG__SERVICE", "https://tokens.example.invalid/issue")
    settings = load_settings(config)
    assert settings.catalog.token is None  # type: ignore[attr-defined]
    assert settings.catalog.service is not None  # type: ignore[attr-defined]
    locked = locked_keys(settings)["catalog"]
    assert set(locked) == {"token", "token_file", "service"}
    assert set(locked.values()) == {"SHIJHON_CATALOG__SERVICE"}
    saved = {"catalog": {"token": Saved("from-the-dashboard", 0.0, "t")}}
    applying = effective(settings, saved, locked=locked_keys(settings))[0].catalog
    assert applying.token is None  # the dashboard's does not win over the environment's choice


def test_the_catalog_is_built_by_its_adapter_from_the_whole_section(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert build_catalog(load_settings(None).catalog) is None
    settings = load_settings(
        None, catalog={"kind": "one", "token": "t", "region": "gb", "timeout_seconds": 4}
    )
    with caplog.at_level("WARNING", logger="shijhon.catalog.setup"):
        built = build_catalog(settings.catalog)
    assert isinstance(built, Built) and built.region == "gb"
    assert built.settings.token.get_secret_value() == "t"
    assert built.settings.cache_seconds == 3600  # Shijhon's own settings too
    assert built.context.timeout_seconds == 4
    assert "Catalog One is made up." in caplog.text  # the adapter's notice, at startup


def test_the_dashboard_s_fields_come_from_the_declaration() -> None:
    fields = {f.key: f for f in sections.fields_of("catalog", "one")}
    assert [f.key for f in sections.fields_of("catalog", "one")][:5] == [
        "kind", "region", "token", "token_file", "service",
    ]  # fmt: skip
    kinds = dict(fields["kind"].options())
    assert kinds["none"] == "None" and kinds["one"] == "Catalog One" and "two" in kinds
    assert fields["token"].kind == "secret" and fields["token_file"].kind == "path"
    assert fields["service_headers"].kind == "mapping" and not fields["service_headers"].editable
    assert fields["region"].meta.short and fields["region"].meta.label == "Region"
    assert fields["rate"].meta.label == "Rate"  # undeclared words: from the key
    assert fields["rate"].meta.group == "advanced" and fields["region"].meta.group == "connection"
    assert "token" not in {f.key for f in sections.fields_of("catalog")}
    assert "rate" not in {f.key for f in sections.fields_of("catalog", "two")}


def _submit(configured: Any, saved: Any, form: dict[str, str]) -> sections.Submitted:
    async def run() -> sections.Submitted:
        return await sections.submit(
            "catalog", configured, saved, form, addon_names=[], locked=locked_keys(configured)
        )

    return anyio.run(run)


def _rows(configured: Any, saved: Any, **more: Any) -> dict[str, dict[str, Any]]:
    groups = sections.rows(
        "catalog", configured, saved, addon_names=[], locked=locked_keys(configured), **more
    )
    return {row["name"]: row for _, rows in groups for row in rows}


def test_choosing_a_catalog_in_the_dashboard() -> None:
    """A catalog that cannot be built is not saved: the page shows its settings with what
    is missing; with them given - in the form made for that catalog - the kind and its
    settings are saved together, with the kind they are for."""
    configured = load_settings(None)
    assert "token" not in _rows(configured, {})  # none chosen: no adapter's settings
    refused = _submit(configured, {}, {"kind": "one", SHOWN_KIND: "none"})
    assert refused.problems["token"].row == "Set a token." and refused.candidate is not None
    assert refused.first_step  # (its settings were not on the form: the page for them next)
    shown = _rows(configured, {}, form={"kind": "one"}, problems=refused.problems)
    assert shown["kind"]["chosen"] == "one" and shown["token"]["problem"] == "Set a token."
    assert shown["region"]["text"] == "xx"  # the adapter's default
    unknown = _submit(configured, {}, {"kind": "elsewhere"})
    assert "kind" in unknown.problems
    # A value typed into a form that was not made for this catalog is not its setting.
    stray = _submit(configured, {}, {"kind": "one", "token": "tok-1", SHOWN_KIND: "none"})
    assert stray.problems["token"].row == "Set a token." and stray.first_step
    missing = _submit(configured, {}, {"kind": "one", "region": "GB", SHOWN_KIND: "one"})
    assert missing.problems["token"].row == "Set a token." and not missing.first_step
    form = {"kind": "one", "token": "tok-1", "region": "GB", SHOWN_KIND: "one"}
    accepted = _submit(configured, {}, form)
    assert not accepted.problems
    written = sections.writes("catalog", configured, {}, accepted, locked=locked_keys(configured))
    assert written.changed == ["kind", "region", "token"]
    assert written.writes == {
        "kind": "one", "region": "gb", "token": SecretStr("tok-1"), ADAPTER: "one",
    }  # fmt: skip
    # A catalog without anything to set is saved at once.
    bare = _submit(configured, {}, {"kind": "bare"})
    assert not bare.problems
    assert sections.writes(
        "catalog", configured, {}, bare, locked=locked_keys(configured)
    ).writes == {"kind": "bare"}


def test_a_catalog_that_needs_what_cannot_be_entered_is_refused_at_once() -> None:
    """Choosing a catalog is a first step only when what is missing can be entered on its
    page: a setting the page only shows (a mapping, set in the configuration file) is a
    refusal straight away."""

    class MappedSettings(BaseModel):
        headers: dict[str, str] = Field(default_factory=dict)

    def needs_headers(settings: Any) -> Problem | None:
        return None if settings.headers else Problem("headers", "Set them.", "are needed.")

    plugin.register(
        "mapped",
        Adapter(label="Mapped", build=Built, settings=MappedSettings, problem=needs_headers),
    )
    try:
        refused = _submit(load_settings(None), {}, {"kind": "mapped", SHOWN_KIND: "none"})
        assert list(refused.problems) == ["headers"] and not refused.first_step
    finally:
        plugin.unregister("mapped")


def test_a_form_made_for_one_catalog_never_shows_its_secret_as_another_s_text() -> None:
    """One's ``token`` is a secret; Plain declares a plain-text ``token`` and needs a
    ``name``. A token typed into One's form, sent with Plain chosen, is refused - and the
    page shown again has Plain's fields without the typed secret."""

    class PlainSettings(BaseModel):
        token: str = ""
        name: str = ""

    def needs_a_name(settings: Any) -> Problem | None:
        return None if settings.name else Problem("name", "Enter a name.", "is needed.")

    plugin.register(
        "plain", Adapter(label="Plain", build=Built, settings=PlainSettings, problem=needs_a_name)
    )
    try:
        configured = load_settings(None, catalog={"kind": "one", "token": "file-token"})
        form = {"kind": "plain", "token": "typed-SECRET", SHOWN_KIND: "one"}
        refused = _submit(configured, {}, form)
        assert refused.problems["name"].row == "Enter a name."
        assert refused.candidate is not None and refused.candidate.token == ""  # type: ignore[attr-defined]
        shown = _rows(configured, {}, form=form, problems=refused.problems)
        assert shown["token"]["kind"] == "text" and shown["token"]["text"] == ""
        assert "SECRET" not in repr(shown) and "file-token" not in repr(shown)
    finally:
        plugin.unregister("plain")


def test_another_catalog_gets_none_of_the_first_one_s_settings() -> None:
    """Both adapters have a ``token``: one saved (or configured) for the first never reaches
    the second - not when the second is chosen in the dashboard, nor when the configuration
    changes the kind under values saved for the first, nor after a later save."""
    configured = load_settings(None, catalog={"kind": "one", "token": "file-token-ONE"})
    saved = {
        "catalog": {
            "token": Saved("saved-token-ONE", 0.0, "t"),
            "twins": Saved("clean", 0.0, "t"),
            ADAPTER: Saved("one", 0.0, "t"),
        }
    }
    locked = locked_keys(configured)
    applying = effective(configured, saved, locked=locked)[0].catalog
    assert applying.token.get_secret_value() == "saved-token-ONE"  # type: ignore[attr-defined]
    # Chosen in the dashboard: its own settings start empty, the first one's are removed.
    switched = _submit(configured, saved, {"kind": "two", SHOWN_KIND: "one"})
    assert not switched.problems and switched.candidate is not None
    assert switched.candidate.token is None  # type: ignore[attr-defined]
    assert switched.candidate.twins == "clean"  # Shijhon's own settings stay
    written = sections.writes("catalog", configured, saved, switched, locked=locked)
    assert written.writes == {"kind": "two", "token": REMOVE, ADAPTER: REMOVE}
    assert written.changed == ["kind"]
    # The same token typed for the second one (in its own form) is saved for it.
    typed = _submit(
        configured, saved, {"kind": "two", "token": "saved-token-ONE", SHOWN_KIND: "two"}
    )
    written = sections.writes("catalog", configured, saved, typed, locked=locked)
    assert written.writes == {
        "kind": "two", "token": SecretStr("saved-token-ONE"), ADAPTER: "two",
    }  # fmt: skip
    # A saved kind: the configuration's adapter settings are not the other catalog's.
    saved_kind = {"catalog": {"kind": Saved("two", 0.0, "t")}}
    other = effective(configured, saved_kind, locked=locked)[0].catalog
    assert other.kind == "two" and other.token is None  # type: ignore[attr-defined]
    # The file now names the second: what was saved for the first is not applied to it ...
    moved = load_settings(None, catalog={"kind": "two"})
    after = effective(moved, saved, locked=locked_keys(moved))[0].catalog
    assert after.kind == "two" and after.token is None  # type: ignore[attr-defined]
    assert after.twins == "clean"
    rows = _rows(moved, saved)
    assert rows["token"]["state"] == ("unset", "")  # nothing saved for this catalog
    # ... nor after a save of something else: the first one's values go, and are not marked
    # as the second's.
    later = _submit(moved, saved, {"cache_seconds": "60", SHOWN_KIND: "two"})
    assert not later.problems and later.candidate.token is None  # type: ignore[union-attr]
    written = sections.writes("catalog", moved, saved, later, locked=locked_keys(moved))
    assert written.writes == {"cache_seconds": 60.0, "token": REMOVE, ADAPTER: REMOVE}


def test_saved_adapter_settings_without_a_recorded_kind_are_nobody_s() -> None:
    """Saved adapter settings without a recorded kind have no owner: no kind gets them
    (Shijhon's own saved settings apply), and a save on a catalog's page removes them."""
    configured = load_settings(None, catalog={"kind": "one", "token": "file-token"})
    unmarked = {"catalog": {"token": Saved("other", 0.0, "t"), "twins": Saved("clean", 0, "t")}}
    kept = effective(configured, unmarked, locked={})[0].catalog
    assert kept.token.get_secret_value() == "file-token"  # type: ignore[attr-defined]
    assert kept.twins == "clean"
    later = _submit(configured, unmarked, {"cache_seconds": "60", SHOWN_KIND: "one"})
    written = sections.writes("catalog", configured, unmarked, later, locked={})
    assert written.writes == {"cache_seconds": 60.0, "token": REMOVE}


def _with_env(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> Any:
    monkeypatch.setenv(name, value)
    try:
        return load_settings(None)
    finally:
        monkeypatch.delenv(name)


def test_an_environment_kind_does_not_get_the_file_s_adapter_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is written for One; the environment chooses Two. The file's ``token`` is
    One's: Two is built without it (Shijhon's own settings in the file still apply)."""
    config = tmp_path / "shijhon.toml"
    config.write_text('[catalog]\nkind = "one"\ntoken = "one-SECRET"\ntwins = "clean"\n')
    assert load_settings(config).catalog.token is not None  # type: ignore[attr-defined]
    monkeypatch.setenv("SHIJHON_CATALOG__KIND", "two")
    settings = load_settings(config)
    assert settings.catalog.kind == "two" and settings.catalog.twins == "clean"
    assert settings.catalog.token is None  # type: ignore[attr-defined]
    monkeypatch.setenv("SHIJHON_CATALOG__TOKEN", "two-token")  # the environment's is Two's
    token = load_settings(config).catalog.token  # type: ignore[attr-defined]
    assert token.get_secret_value() == "two-token"
    monkeypatch.delenv("SHIJHON_CATALOG__KIND")
    monkeypatch.delenv("SHIJHON_CATALOG__TOKEN")
    given = load_settings(config, catalog={"kind": "two"})  # so do overrides
    assert given.catalog.token is None  # type: ignore[attr-defined]


def test_the_file_is_read_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whose the file's settings are and what they are come from one reading: a file changed
    while it is loaded - now naming One, with One's token - cannot hand that token to the
    environment's Two."""
    from shijhon import config as module

    path = tmp_path / "shijhon.toml"
    path.write_text('[catalog]\nkind = "two"\ntoken = "two-token"\n')
    readings = 0

    class Changing(module.TomlConfigSettingsSource):
        def _read_file(self, file_path: Any) -> dict[str, Any]:
            nonlocal readings
            readings += 1
            data: dict[str, Any] = super()._read_file(file_path)
            path.write_text('[catalog]\nkind = "one"\ntoken = "one-SECRET"\n')  # meanwhile
            return data

    monkeypatch.setattr(module, "TomlConfigSettingsSource", Changing)
    monkeypatch.setenv("SHIJHON_CATALOG__KIND", "two")
    settings = load_settings(path)
    assert readings == 1 and settings.catalog.kind == "two"
    assert settings.catalog.token.get_secret_value() == "two-token"  # type: ignore[attr-defined]
    assert load_settings(tmp_path / "missing.toml").catalog.kind == "two"  # no file: defaults


def test_adapter_settings_without_a_kind_are_nobody_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configuration that holds an adapter's settings without naming a kind, or an
    environment that sets them without one: they have no owner, so no kind gets them,
    however it is chosen (Shijhon's own settings in the file still apply)."""
    config = tmp_path / "shijhon.toml"
    config.write_text(
        '[catalog]\nregion = "de"\ntoken = "file-token"\ntwins = "clean"\n'
        'service = "https://one.example.invalid/"\n'
        '[catalog.service_headers]\nx-key = "header-SECRET"\n'
    )
    configured = load_settings(config)
    assert configured.catalog.kind == "none" and not hasattr(configured.catalog, "token")
    assert "file-token" not in repr(configured) and "file-token" not in repr(configured.catalog)
    saved = {"catalog": {"kind": Saved("one", 0.0, "t")}}
    locked = locked_keys(configured)
    applying = effective(configured, saved, locked=locked)[0].catalog
    assert applying.kind == "one" and applying.twins == "clean"
    assert applying.token is None and applying.region == "xx"  # type: ignore[attr-defined]
    assert applying.service_headers == {}  # type: ignore[attr-defined]
    assert _rows(configured, saved)["token"]["state"] == ("unset", "")
    monkeypatch.setenv("SHIJHON_CATALOG__KIND", "one")  # One, named there: its own
    assert load_settings(config).catalog.token is None  # type: ignore[attr-defined]
    monkeypatch.delenv("SHIJHON_CATALOG__KIND")
    # The environment's adapter settings without a kind: nobody's either.
    monkeypatch.setenv("SHIJHON_CATALOG__TOKEN", "env-token")
    configured = load_settings(None)
    applying = effective(configured, saved, locked=locked_keys(configured))[0].catalog
    assert applying.kind == "one" and applying.token is None  # type: ignore[attr-defined]
    # A file that names a kind: its settings are that kind's, also when the kind is switched
    # off above it - a kind chosen in the dashboard then does not find them.
    monkeypatch.delenv("SHIJHON_CATALOG__TOKEN")
    config.write_text('[catalog]\nkind = "two"\ntoken = "two-SECRET"\n')
    off = load_settings(config, catalog={"kind": "none"})
    assert off.catalog.kind == "none"
    taken = effective(off, {"catalog": {"kind": Saved("one", 0.0, "t")}}, locked=locked_keys(off))
    assert taken[0].catalog.kind == "one"
    assert taken[0].catalog.token is None  # type: ignore[attr-defined]


def test_the_environment_s_lists_and_mappings_are_read_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment's value of an adapter's mapping (or list) is read as JSON."""
    monkeypatch.setenv("SHIJHON_CATALOG__KIND", "one")
    monkeypatch.setenv("SHIJHON_CATALOG__SERVICE_HEADERS", '{"x-key": "header-SECRET"}')
    monkeypatch.setenv("SHIJHON_CATALOG__TOKEN", "env-token")
    headers = load_settings(None).catalog.service_headers  # type: ignore[attr-defined]
    assert {key: value.get_secret_value() for key, value in headers.items()} == {
        "x-key": "header-SECRET"
    }


def test_the_configured_adapter_s_environment_locks_nothing_of_another_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file's kind is One with a token from the environment; Two is chosen in the
    dashboard: its ``token`` is its own - neither locked by One's variable nor filled."""
    monkeypatch.setenv("SHIJHON_CATALOG__KIND", "one")
    monkeypatch.setenv("SHIJHON_CATALOG__TOKEN", "one-env-token")
    monkeypatch.setenv("SHIJHON_CATALOG__TWINS", "clean")
    configured = load_settings(None)
    locked = locked_keys(configured)
    assert "token" in locked["catalog"] and "kind" in locked["catalog"]
    # (The kind itself is the environment's: a saved kind does not apply.)
    saved = {"catalog": {"kind": Saved("two", 0.0, "t")}}
    assert effective(configured, saved, locked=locked)[0].catalog.kind == "one"
    monkeypatch.delenv("SHIJHON_CATALOG__KIND")
    configured = load_settings(None, catalog={"kind": "one"})
    locked = locked_keys(configured)
    applying = effective(configured, saved, locked=locked)[0].catalog
    assert applying.kind == "two" and applying.token is None  # type: ignore[attr-defined]
    rows = _rows(configured, saved)
    assert rows["token"]["kind"] == "secret" and rows["twins"]["kind"] == "locked"
    typed = _submit(configured, saved, {"token": "two-token", SHOWN_KIND: "two"})
    assert typed.values == {"token": SecretStr("two-token")} and not typed.problems


def test_a_saved_kind_whose_adapter_is_gone_leaves_the_configuration_s() -> None:
    configured = load_settings(None)
    saved = {"catalog": {"kind": Saved("removed", 0.0, "t"), "token": Saved("tok-SECRET", 0, "t")}}
    applying, problems = effective(configured, saved, locked={}, startup=True)
    assert applying.catalog.kind == "none"
    assert "no catalog adapter named 'removed'" in problems["catalog"]
    assert "SECRET" not in problems["catalog"]


def test_what_cannot_be_built_at_startup_is_not_applied() -> None:
    configured = load_settings(None)
    saved = {"catalog": {"kind": Saved("one", 0.0, "t")}}  # no token: One cannot be built
    applying, problems = effective(configured, saved, locked={}, startup=True)
    assert applying.catalog.kind == "none" and "catalog.token" in problems["catalog"]
    assert effective(configured, saved, locked={})[0].catalog.kind == "one"  # the page's view


def test_what_the_catalog_would_not_get_is_not_counted_at_startup() -> None:
    """A catalog that needs what is bound to its address: with the address saved on
    another host, the bound value is held back before the check - so the saved section is
    refused at startup, not handed to the adapter without it."""

    class KeyedSettings(BaseModel):
        service: SecretStr | None = None
        service_key: SecretStr | None = None

    def needs_its_key(settings: Any) -> Problem | None:
        if settings.service_key is None:
            return Problem("service_key", "Set the key.", "is needed.")
        return None

    plugin.register(
        "keyed",
        Adapter(
            label="Keyed",
            build=Built,
            settings=KeyedSettings,
            bound={"service_key": "service"},
            problem=needs_its_key,
        ),
    )
    try:
        configured = load_settings(
            None,
            catalog={
                "kind": "keyed",
                "service": "https://one.example.invalid/",
                "service_key": "key-SECRET",
            },
        )
        saved = {
            "catalog": {
                "service": Saved("https://two.example.invalid/", 0.0, "t"),
                ADAPTER: Saved("keyed", 0.0, "t"),
            }
        }
        applying, problems = effective(configured, saved, locked={}, startup=True)
        assert "catalog.service_key" in problems["catalog"] and "SECRET" not in str(problems)
        assert applying.catalog.service.get_secret_value().startswith("https://one.")  # type: ignore[attr-defined]
        refused = _submit(configured, {}, {"service": "https://two.example.invalid/"})
        assert refused.problems["service_key"].row == "Set the key."
        # The key saved in the dashboard itself goes with the address it was saved for.
        saved["catalog"]["service_key"] = Saved("dashboard-key", 0.0, "t")
        saved["catalog"][BOUND] = Saved(
            {"service_key": place("https://two.example.invalid/")}, 0.0, "t"
        )
        applying, problems = effective(configured, saved, locked={}, startup=True)
        assert not problems
        assert applying.catalog.service_key.get_secret_value() == "dashboard-key"  # type: ignore[attr-defined]
        assert not withheld(configured.catalog, applying.catalog)
    finally:
        plugin.unregister("keyed")


class KeyedSettings(BaseModel):
    service: str | None = None
    service_key: SecretStr | None = None


@pytest.fixture
def keyed() -> Iterator[None]:
    """A catalog with a key for its service, both editable in the dashboard."""
    words = {"service": Words("Service"), "service_key": Words("Service key")}
    adapter = Adapter(
        label="Keyed",
        build=Built,
        settings=KeyedSettings,
        words=words,
        bound={"service_key": "service"},
    )
    plugin.register("keyed", adapter)
    try:
        yield
    finally:
        plugin.unregister("keyed")


def _save(configured: Any, saved: dict[str, Any], form: dict[str, str]) -> sections.Written:
    """Submit and write a Catalog form, as the dashboard does (``saved`` is changed)."""
    submitted = _submit(configured, saved, {SHOWN_KIND: "keyed", **form})
    assert not submitted.problems
    written = sections.writes(
        "catalog", configured, saved, submitted, locked=locked_keys(configured)
    )
    items = saved.setdefault("catalog", {})
    for key, value in written.writes.items():
        if value is REMOVE:
            items.pop(key, None)
        else:
            shown = value.get_secret_value() if isinstance(value, SecretStr) else value
            items[key] = Saved(shown, 0.0, "t")
    return written


def _key(configured: Any, saved: Any) -> str | None:
    found = effective(configured, saved, locked=locked_keys(configured))[0].catalog
    return found.service_key.get_secret_value() if found.service_key is not None else None  # type: ignore[attr-defined]


def test_a_saved_secret_stays_with_the_address_it_was_saved_for(keyed: None) -> None:
    """A key saved in the dashboard is its service's alone. A later
    change of the service's address to another host - saved here - removes it, unless it is
    entered again with the address; a change of the path keeps it."""
    configured = load_settings(None, catalog={"kind": "keyed"})
    saved: dict[str, Any] = {}
    one, two = "https://one.example.invalid/api", "https://two.example.invalid/api"
    written = _save(configured, saved, {"service": one, "service_key": "key-ONE"})
    assert written.writes[BOUND] == {"service_key": place(one)} and not written.displaced
    assert "example" not in str(written.writes[BOUND])  # a mark, not the address
    assert _key(configured, saved) == "key-ONE"
    # Another path on the same host: the key stays.
    written = _save(configured, saved, {"service": "https://ONE.example.invalid:443/v2"})
    assert BOUND not in written.writes and "service_key" not in written.writes
    assert _key(configured, saved) == "key-ONE"
    # Another host, the key not entered again: it does not follow, and is removed.
    moved = _submit(configured, saved, {SHOWN_KIND: "keyed", "service": two})
    assert moved.candidate is not None and moved.candidate.service_key is None  # type: ignore[attr-defined]
    written = _save(configured, saved, {"service": two})
    assert written.displaced == [("service_key", "service")]
    assert written.writes["service_key"] is REMOVE and written.writes[BOUND] is REMOVE
    assert "service_key" in written.changed
    assert set(saved["catalog"]) == {"service", ADAPTER}
    assert _key(configured, saved) is None
    row = _rows(configured, saved)["service_key"]
    assert row["state"] == ("unset", "")
    # Entered again with a new address: it is that address's.
    written = _save(configured, saved, {"service": one, "service_key": "key-ONE"})
    written = _save(configured, saved, {"service": two, "service_key": "key-TWO"})
    assert written.writes[BOUND] == {"service_key": place(two)} and not written.displaced
    assert _key(configured, saved) == "key-TWO"
    # The saved address removed, the configuration's being elsewhere: the key goes too.
    elsewhere = load_settings(
        None, catalog={"kind": "keyed", "service": "https://three.example.invalid/"}
    )
    assert _key(elsewhere, saved) == "key-TWO"  # (the saved address is the one in use)
    del saved["catalog"]["service"]
    assert _key(elsewhere, saved) is None


def test_a_saved_secret_does_not_follow_an_address_changed_outside_the_dashboard(
    keyed: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """a1, the other ways an address changes: the configuration file or the environment.
    The saved key is held back (said at startup and on the page), the page shows it as not
    set, and the next save removes it. A value saved with no place recorded has none."""
    one, two = "https://one.example.invalid/api", "https://two.example.invalid/api"
    configured = load_settings(None, catalog={"kind": "keyed", "service": one})
    saved: dict[str, Any] = {}
    written = _save(configured, saved, {"service_key": "key-ONE"})
    assert written.writes[BOUND] == {"service_key": place(one)}
    locked = locked_keys(configured)
    assert _key(configured, saved) == "key-ONE" and not out_of_place(configured, saved, locked)
    # The file names another host now.
    changed = load_settings(None, catalog={"kind": "keyed", "service": two})
    assert _key(changed, saved) is None
    assert out_of_place(changed, saved, locked) == [("service_key", "service")]
    assert _rows(changed, saved)["service_key"]["state"] == ("unset", "")
    # The environment does (it wins over the file).
    config = tmp_path / "config.toml"
    config.write_text(f'[catalog]\nkind = "keyed"\nservice = "{one}"\n')
    assert _key(load_settings(config), saved) == "key-ONE"
    monkeypatch.setenv("SHIJHON_CATALOG__SERVICE", two)
    by_environment = load_settings(config)
    assert by_environment.catalog.service == two  # type: ignore[attr-defined]
    assert _key(by_environment, saved) is None
    monkeypatch.delenv("SHIJHON_CATALOG__SERVICE")
    # Any save of the page removes the value that is no longer its address's.
    written = _save(changed, saved, {})
    assert written.writes == {"service_key": REMOVE, BOUND: REMOVE, ADAPTER: REMOVE}
    assert written.displaced == [("service_key", "service")]  # said on the page, and logged
    assert "catalog" in saved and not saved["catalog"]
    # Saved before places were recorded: nobody's, until entered again.
    old = {"catalog": {"service_key": Saved("key-OLD", 0.0, "t"), ADAPTER: Saved("keyed", 0, "t")}}
    assert _key(configured, old) is None
    assert out_of_place(configured, old, locked) == [("service_key", "service")]
    # A key saved while there is no address is not the first address's that comes.
    bare = load_settings(None, catalog={"kind": "keyed"})
    saved = {}
    _save(bare, saved, {"service_key": "key-EARLY"})
    assert _key(bare, saved) == "key-EARLY"  # (kept; there is nowhere to send it)
    assert _key(configured, saved) is None
    written = _save(bare, saved, {"service": one})
    assert written.displaced == [("service_key", "service")] and _key(bare, saved) is None


def test_a_removed_credential_stays_removed_whatever_the_address(keyed: None) -> None:
    """A configured key removed in the dashboard is saved as "no value". That is nothing to
    send anywhere: it has no place, and stays - as saved before places were recorded (an
    upgrade never brings the configuration's key back), and when the address changes."""
    one, two = "https://one.example.invalid/api", "https://two.example.invalid/api"
    configured = load_settings(
        None, catalog={"kind": "keyed", "service": one, "service_key": "key-FILE"}
    )
    assert _key(configured, {}) == "key-FILE"
    old = {"catalog": {"service_key": Saved(None, 0.0, "t"), ADAPTER: Saved("keyed", 0, "t")}}
    assert _key(configured, old) is None  # (no place recorded: still removed)
    assert not out_of_place(configured, old, locked_keys(configured))
    assert _rows(configured, old)["service_key"]["state"] == ("unset", "Removed here")
    # Removed on the page now: saved without a place, and kept at the next saves.
    saved: dict[str, Any] = {}
    written = _save(configured, saved, {"service_key-clear": "true"})
    assert written.writes == {"service_key": None, ADAPTER: "keyed"}
    assert _key(configured, saved) is None
    assert _save(configured, saved, {}).writes == {}
    elsewhere = load_settings(
        None, catalog={"kind": "keyed", "service": two, "service_key": "key-FILE"}
    )
    assert _key(elsewhere, saved) is None
    written = _save(configured, saved, {"service": two})
    assert not written.displaced and _key(configured, saved) is None


def test_bound_settings_stay_with_their_host() -> None:
    configured = load_settings(
        None,
        catalog={
            "kind": "one",
            "service": "https://tokens.example.invalid/issue",
            "service_headers": {"x-key": "header-SECRET"},
        },
    )

    def applying(**saved: str) -> Any:
        values = {"catalog": {key: Saved(value, 0.0, "t") for key, value in saved.items()}}
        values["catalog"][ADAPTER] = Saved("one", 0.0, "t")
        return effective(configured, values, locked={})[0].catalog

    same = applying(service="https://tokens.example.invalid/other-path")
    assert set(same.service_headers) == {"x-key"} and not withheld(configured.catalog, same)
    moved = applying(service="https://elsewhere.example.invalid/issue")
    assert moved.service_headers == {}
    assert withheld(configured.catalog, moved) == [("service_headers", "service")]
    # An earlier setting of the choice is set: the service is not used, nothing to say.
    unused = applying(service="https://elsewhere.example.invalid/issue", token="t")
    assert unused.service_headers == {} and not withheld(configured.catalog, unused)
    assert configured.catalog.service_headers  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_saved_adapter_settings_are_listed_without_their_secrets(tmp_path: Path) -> None:
    """``shijhon saved``: an adapter's saved value is shown only when the adapter it was
    saved for is installed and declares it no secret."""

    class OpenSettings(BaseModel):
        token: str = ""  # the same name as One's secret, as plain text
        region: str = ""

    database = tmp_path / "shijhon.sqlite3"
    store = await Store.open(database)
    saved = SavedSettings(store)
    await saved.put(
        "catalog",
        {
            "token": SecretStr("tok-SECRET"),
            "region": "gb",
            "left_behind": "old-SECRET",  # no adapter declares it any more
            ADAPTER: "one",
        },
        "mira",
    )
    listing = list_saved(database)
    assert "SECRET" not in listing
    assert "catalog.token = (secret)" in listing and 'catalog.region = "gb"' in listing
    assert "catalog.left_behind = (secret)" in listing
    assert 'catalog.(adapter) = "one"' in listing
    # A mapping's values may be secrets: never listed.
    await saved.put("catalog", {"service_headers": {"x-key": "header-SECRET"}}, "mira")
    assert "catalog.service_headers = (secret)" in list_saved(database)
    await saved.put("catalog", {"service_headers": REMOVE}, "mira")
    # One is gone; another installed adapter declares its ``token`` as plain text: what was
    # saved for One is still not shown (nor its region: nothing says it is no secret).
    plugin.unregister("one")
    plugin.register("open", Adapter(label="Open", build=Built, settings=OpenSettings))
    try:
        listing = list_saved(database)
        assert "SECRET" not in listing and "catalog.region = (secret)" in listing
        # Without a recorded kind the values have no owner: nothing of them is shown, also
        # with One back.
        await saved.put("catalog", {ADAPTER: REMOVE}, "mira")
        plugin.register("one", ONE)
        listing = list_saved(database)
        assert "catalog.region = (secret)" in listing and "SECRET" not in listing
    finally:
        plugin.unregister("open")
        await store.close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
