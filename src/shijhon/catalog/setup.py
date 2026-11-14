"""Build the configured catalog (or none), and the catalogs asked for artist lists only:
their adapters do (``plugin``)."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from shijhon.catalog import plugin
from shijhon.catalog.base import Catalog
from shijhon.catalog.model import CATALOG_KEY
from shijhon.catalog.paced import PacedCatalog
from shijhon.config import CORE_CATALOG_KEYS, CatalogSettings, catalog_settings
from shijhon.delivery.netpolicy import Resolver, system_resolver

log = logging.getLogger(__name__)


def build_catalog(
    settings: CatalogSettings, *, resolver: Resolver = system_resolver, addons: Any = None
) -> Catalog | None:
    """The catalog of ``settings.kind``, built by its adapter from the section's settings
    (Shijhon's own and the adapter's); None for "none". Raises ``ValueError`` (with a short
    reason, never a value) when the adapter is not installed or cannot build it.
    ``addons``: the installation's add-ons, for the kind that takes its catalog from
    one."""
    if settings.kind == plugin.NONE:
        return None
    adapter = plugin.adapter(settings.kind)
    if adapter.settings is not None and not isinstance(settings, adapter.settings):
        # Settings made without the adapter's own (not through ``load_settings``): at
        # their defaults.
        settings = catalog_settings(settings.kind).model_validate(settings.model_dump())
    if adapter.notice:
        log.warning("catalog: %s", adapter.notice)
    built = adapter.build(
        settings,
        plugin.Context(timeout_seconds=settings.timeout_seconds, resolver=resolver, addons=addons),
    )
    key, region = getattr(built, "key", None), getattr(built, "region", None)
    if not isinstance(key, str) or not CATALOG_KEY.fullmatch(key) or not isinstance(region, str):
        raise ValueError(
            f"the catalog adapter {settings.kind!r} built a catalog without a usable key"
            " (lower-case letters and digits, starting with a letter) or region"
        )
    return built


def artist_list_catalogs(
    settings: CatalogSettings, *, per_second: float, resolver: Resolver = system_resolver
) -> list[tuple[str, PacedCatalog]]:
    """The catalogs asked for artist lists only (``artist_lists_catalogs``), by kind, in
    the setting's order. Each is built by its adapter with Shijhon's own ``[catalog]``
    settings and the adapter's own at their defaults (the section's adapter settings are
    the configured kind's), and asked at ``per_second`` at most (the library pass's pace;
    an adapter's own pacing holds within it). A kind that is not installed, is the
    configured catalog's, or cannot be built is logged and passed over. The caller closes
    each one's ``inner`` catalog."""
    own = {key: getattr(settings, key) for key in CORE_CATALOG_KEYS}
    built: list[tuple[str, PacedCatalog]] = []
    for kind in settings.artist_lists_catalogs:
        if kind in (settings.kind, plugin.NONE):
            if kind != plugin.NONE:
                log.info("artist lists: %r is the configured catalog, asked as that", kind)
            continue
        try:
            adapter = plugin.adapter(kind)  # (not installed: it says what is)
            try:
                theirs = catalog_settings(kind).model_validate({**own, "kind": kind})
            except ValidationError:  # (its message would repeat values)
                raise ValueError("its default settings are not valid") from None
            problem = adapter.problem(theirs) if adapter.problem is not None else None
            if problem is not None:  # (it needs a setting: an account, a token)
                raise ValueError(f"{problem.setting} {problem.notice}")
            catalog = build_catalog(theirs, resolver=resolver)
        except Exception as exc:  # one catalog that cannot be built never stops a start
            plain = isinstance(exc, ValueError) and not isinstance(exc, ValidationError)
            reason = str(exc) if plain else type(exc).__name__
            log.warning("artist lists: the catalog %r is not asked: %s", kind, reason)
            continue
        if catalog is not None:
            built.append((kind, PacedCatalog(catalog, per_second)))
    return built
