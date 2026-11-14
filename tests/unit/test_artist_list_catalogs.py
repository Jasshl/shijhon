"""More catalogs asked for artist lists (``[catalog] artist_lists_catalogs``): built by their
adapters with their own settings at the defaults, each at its own pace, asked after the
configured catalog and before the add-ons, counted in the disagreement and one-artist rules
like any source; one not installed or that cannot be built is logged and passed over."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic import BaseModel

from shijhon.artist_lists.sources import Skips, Sources, Subject, Unavailable
from shijhon.catalog import plugin
from shijhon.catalog.base import CatalogError
from shijhon.catalog.paced import PacedCatalog
from shijhon.catalog.plugin import Adapter, Problem
from shijhon.catalog.setup import artist_list_catalogs
from shijhon.config import CatalogSettings, catalog_settings, load_settings
from shijhon.dashboard.fields import Invalid
from shijhon.dashboard.sections import fields_of
from shijhon.store import Store
from tests.unit.test_artist_list_sources import JOINED, SUBJECT, Addons, Catalog, Slow, track

pytestmark = pytest.mark.anyio

KIND = "tallybook"  # an invented adapter
OTHER = "the tallybook catalog"
BOTH = ("Ash Vane", "Bell Orrin")


class ShelfSettings(BaseModel):
    shelf: str = "front"  # a setting the configured catalog's adapter has too


class Built(Catalog):
    """The invented adapter's catalog: what it was built with, whether it was closed."""

    key = "tally"
    region = ""

    def __init__(self, settings: Any) -> None:
        super().__init__()
        self.settings = settings
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


built: list[Built] = []


def build(settings: Any, context: plugin.Context) -> Built:
    catalog = Built(settings)
    built.append(catalog)
    return catalog


def refuse(settings: Any, context: plugin.Context) -> Built:
    raise ValueError("it needs an account")


def crash(settings: Any, context: plugin.Context) -> Built:
    raise RuntimeError("secret-value-never-logged")


def needs(settings: Any) -> Problem:
    return Problem("shelf", "Enter a shelf.", "is needed to ask it.")


@pytest.fixture(autouse=True)
def adapters(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.upper().startswith("SHIJHON_"):
            monkeypatch.delenv(name)
    plugin.register(KIND, Adapter(label="Tally Book", build=build, settings=ShelfSettings))
    plugin.register("ledger", Adapter(label="Ledger", build=build, settings=ShelfSettings))
    plugin.register("refusing", Adapter(label="Refusing", build=refuse))
    plugin.register("crashing", Adapter(label="Crashing", build=crash))
    plugin.register("needy", Adapter(label="Needy", build=build, problem=needs))
    built.clear()
    yield
    for name in (KIND, "ledger", "refusing", "crashing", "needy"):
        plugin.unregister(name)


def section(**values: Any) -> CatalogSettings:
    kind = values.get("kind", "none")
    return catalog_settings(kind).model_validate(values)


# --- building them ------------------------------------------------------------------------


def test_each_is_built_with_its_own_settings_at_their_defaults_and_paced() -> None:
    # The configured catalog's adapter has a setting of the same name: not handed over.
    settings = section(
        kind="ledger", shelf="back", timeout_seconds=7.0, artist_lists_catalogs=[KIND]
    )
    found = artist_list_catalogs(settings, per_second=2.0)
    assert [kind for kind, _ in found] == [KIND]
    paced = found[0][1]
    assert isinstance(paced, PacedCatalog) and paced.per_second == 2.0
    assert paced.inner is built[-1] and built[-1].settings.kind == KIND
    assert built[-1].settings.shelf == "front"  # its default, not the configured kind's
    assert built[-1].settings.timeout_seconds == 7.0  # (Shijhon's own settings apply)


def test_one_not_installed_or_not_buildable_is_logged_once_and_passed_over(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="shijhon.catalog.setup")
    kinds = ["absent", "refusing", "crashing", "needy", "ledger", KIND]
    found = artist_list_catalogs(
        section(kind="ledger", artist_lists_catalogs=kinds), per_second=1.0
    )
    assert [kind for kind, _ in found] == [KIND]  # (the configured kind: asked as that)
    lines = [r.getMessage() for r in caplog.records]
    assert sum("'absent' is not asked: no catalog adapter named" in line for line in lines) == 1
    assert "artist lists: the catalog 'refusing' is not asked: it needs an account" in lines
    assert "artist lists: the catalog 'crashing' is not asked: RuntimeError" in lines
    assert "artist lists: the catalog 'needy' is not asked: shelf is needed to ask it." in lines
    assert not any("secret-value" in line for line in lines)
    assert any("'ledger' is the configured catalog" in line for line in lines)


def test_the_setting_keeps_each_kind_once_and_never_refuses_one() -> None:
    settings = load_settings(
        None, catalog={"artist_lists_catalogs": [" Tallybook ", "tallybook", "", "absent"]}
    )
    assert settings.catalog.artist_lists_catalogs == [KIND, "absent"]
    assert CatalogSettings().artist_lists_catalogs == []


async def test_their_pace_holds_whatever_the_adapter_does() -> None:
    (_, paced), *_ = artist_list_catalogs(section(artist_lists_catalogs=[KIND]), per_second=20.0)
    started = anyio.current_time()
    for _ in range(4):
        await paced.songs_by_isrc("QZAAA2600001")
    assert anyio.current_time() - started >= 0.12  # (one each 0.05 s after the first)
    assert paced.requests == 4


def test_the_dashboard_shows_it_next_to_the_artist_lists_settings() -> None:
    fields = {f.key: f for f in fields_of("catalog")}
    field = fields["artist_lists_catalogs"]
    assert (field.kind, field.meta.group) == ("names", fields["artist_lists"].meta.group)
    keys = list(fields)
    assert keys.index("artist_lists_catalogs") == keys.index("artist_lists_library") + 1
    assert field.parse("musicbrainz, Other_one;musicbrainz") == ["musicbrainz", "other_one"]
    assert field.parse("  ") == []
    with pytest.raises(Invalid):
        field.parse("not a/name")
    assert field.show(["musicbrainz", "other"]) == "musicbrainz, other"
    assert (field.describe([]), field.describe(["musicbrainz"])) == ("none", "musicbrainz")


# --- asking them --------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    opened = await Store.open(tmp_path / "state.db")
    try:
        yield opened
    finally:
        await opened.close()


TEXT = Subject("s1", "Ash Vane, Bell Orrin & Cora Dune", isrcs=("QZAAA2600001",))


async def test_they_are_asked_after_the_catalog_and_before_the_add_ons(store: Store) -> None:
    main = Slow(0.2, isrc=[track("c1", artists=("Ash Vane, Bell Orrin", "Cora Dune"))])
    extra = Slow(0.1, isrc=[track("x1", artists=("Ash Vane", "Bell Orrin", "Cora Dune"))])
    addon = Slow(0.0, isrc=[track("i1", artists=("Ash Vane", "Bell Orrin & Cora Dune"))])
    sources = Sources(store, main, Addons(addon), [(KIND, extra)])
    started = anyio.current_time()
    lists, failed, answered = await sources.by_isrc(TEXT)
    assert anyio.current_time() - started < 0.3  # (together)
    assert [(asked.name, names) for asked, names in lists] == [
        ("the catalog", ("Ash Vane, Bell Orrin", "Cora Dune")),
        (OTHER, ("Ash Vane", "Bell Orrin", "Cora Dune")),
        ("add-on 0", ("Ash Vane", "Bell Orrin & Cora Dune")),
    ]
    assert (failed, answered) == ([], True)


async def test_its_list_is_named_by_its_kind(store: Store) -> None:
    extra = Catalog(isrc=[track("x1", artists=BOTH)])
    found = await Sources(store, None, Addons(), [(KIND, extra)]).resolve(SUBJECT)
    assert (found.state, found.names, found.source) == ("resolved", BOTH, "tallybook_isrc")
    # Its search names no artists: each candidate is asked for again.
    searched = Catalog(search=[track("x2")], songs={"x2": track("x2", artists=BOTH)})
    found = await Sources(store, None, Addons(), [(KIND, searched)]).resolve(SUBJECT)
    assert found.source == "tallybook_search" and "song:x2" in searched.asked
    # The configured catalog's list goes first when both agree.
    main = Catalog(isrc=[track("c1", artists=BOTH)])
    found = await Sources(store, main, Addons(), [(KIND, extra)]).resolve(SUBJECT)
    assert found.source == "catalog_isrc"


async def test_it_and_the_catalog_that_differ_are_ambiguous(store: Store) -> None:
    main = Catalog(isrc=[track("c1", artists=("Ash Vane, Bell Orrin", "Cora Dune"))])
    extra = Catalog(isrc=[track("x1", artists=("Ash Vane", "Bell Orrin", "Cora Dune"))])
    found = await Sources(store, main, Addons(), [(KIND, extra)]).resolve(TEXT)
    assert (found.state, found.source) == ("ambiguous", "lists_differ")
    # ... and its one artist of the whole text vetoes the catalog's split.
    band = Catalog(isrc=[track("x2", artists=(JOINED,))])
    main = Catalog(isrc=[track("c2", artists=BOTH)])
    found = await Sources(store, main, Addons(), [(KIND, band)]).resolve(SUBJECT)
    assert (found.state, found.vetoed_by) == ("ambiguous", (OTHER,))


async def test_its_failure_counts_like_an_add_on_s(store: Store) -> None:
    down = Catalog(fail="all")
    # The configured catalog's own list counts while it is down ...
    main = Catalog(isrc=[track("c1", artists=BOTH)])
    found = await Sources(store, main, Addons(), [(KIND, down)]).resolve(SUBJECT)
    assert (found.state, found.source) == ("resolved", "catalog_isrc")
    # ... an add-on's waits for it, and its own list waits for the catalog and the add-ons.
    up = Catalog(isrc=[track("i1", artists=BOTH)])
    with pytest.raises(Unavailable) as later:
        await Sources(store, None, Addons(up), [(KIND, down)]).resolve(SUBJECT)
    assert later.value.answered
    extra = Catalog(isrc=[track("x1", artists=BOTH)])
    with pytest.raises(Unavailable):
        await Sources(store, Catalog(fail="all"), Addons(), [(KIND, extra)]).resolve(SUBJECT)
    with pytest.raises(Unavailable):
        await Sources(store, None, Addons(Catalog(fail="all")), [(KIND, extra)]).resolve(SUBJECT)


async def test_one_that_keeps_failing_is_skipped_for_the_pass_and_is_no_add_on(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="shijhon.artist_lists.sources")
    down = Catalog(fail="all")
    main = Catalog(isrc=[track("c1", artists=BOTH)])
    sources, skips = Sources(store, main, Addons(), [(KIND, down)]), Skips()
    for _ in range(3):
        found = await sources.resolve(SUBJECT, link=False, skips=skips)
        assert found.source == "catalog_isrc"
    assert down.asked == ["isrc", "isrc"]  # (no more once skipped)
    assert skips.skipped == {OTHER: "down"} and not skips.addons
    lines = [r.getMessage() for r in caplog.records]
    assert lines.count(f"artist lists: {OTHER} skipped for this pass: down") == 1


async def test_a_failure_of_its_search_is_a_failure_not_an_empty_answer(store: Store) -> None:
    class Searching(Catalog):
        async def search(self, term: str, limit: int = 20) -> Any:
            raise CatalogError("rate_limited", "slow down")

    with pytest.raises(Unavailable):
        await Sources(store, None, Addons(), [(KIND, Searching())]).resolve(SUBJECT)


# --- the application ---------------------------------------------------------------------


async def test_the_application_asks_them_and_closes_them(tmp_path: Path) -> None:
    from shijhon.app import ShijhonApp

    settings = load_settings(
        None,
        state_dir=tmp_path / "state",
        navidrome={"url": "http://127.0.0.1:9", "library_path": tmp_path},
        catalog={"artist_lists_catalogs": [KIND, "absent"]},
    )
    app = ShijhonApp(settings)
    await app.startup()
    try:
        lists = app.services.artist_lists
        assert lists is not None
        others = lists.sources.others
        assert [(a.name, a.prefix) for a in others] == [(OTHER, "tallybook_")]
        assert isinstance(others[0].catalog, PacedCatalog)
        assert others[0].catalog.inner is built[-1]
    finally:
        with anyio.fail_after(10):
            await app.shutdown()
    assert built[-1].closed
