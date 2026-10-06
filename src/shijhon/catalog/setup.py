"""Build the configured catalog (or none): its adapter does (``plugin``)."""

from __future__ import annotations

import logging
from typing import Any

from shijhon.catalog import plugin
from shijhon.catalog.base import Catalog
from shijhon.catalog.model import CATALOG_KEY
from shijhon.config import CatalogSettings, catalog_settings
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
