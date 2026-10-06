"""Live catalog smoke test (opt-in).

Runs only with ``-m live`` and ``SHIJHON_LIVE_CONFIG`` pointing at a private configuration
with a ``[catalog]`` section, and ``SHIJHON_LIVE_SEARCH`` naming a search term. A handful
of requests through the real adapter: one search, the first album found (with tracks), its
artist and discography, and one artwork image; then the same album again from the cache.
Only counts, kinds and timings are printed — never names, tokens or URLs.
"""

from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

import anyio
import pytest

from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.editions import dedupe_editions
from shijhon.catalog.model import artwork_url
from shijhon.catalog.setup import build_catalog
from shijhon.config import CatalogSettings, catalog_settings

pytestmark = pytest.mark.live
CONFIG = os.environ.get("SHIJHON_LIVE_CONFIG")
TERM = os.environ.get("SHIJHON_LIVE_SEARCH")


@pytest.mark.skipif(not CONFIG or not TERM, reason="SHIJHON_LIVE_CONFIG/SEARCH not set")
def test_real_catalog() -> None:
    assert CONFIG is not None and TERM is not None
    section = tomllib.loads(Path(CONFIG).read_text()).get("catalog", {})
    # The section with its adapter's settings (the adapter must be installed).
    settings: CatalogSettings = catalog_settings(str(section.get("kind", "none"))).model_validate(
        section
    )

    async def main() -> None:
        inner = build_catalog(settings)
        assert inner is not None, "no [catalog] section"
        catalog = CachedCatalog(inner, ttl=600)
        try:
            started = time.monotonic()
            found = await catalog.search(TERM, 10)
            print(f"search: {len(found.albums)} albums, {len(found.songs)} songs"
                  f" in {time.monotonic() - started:.2f}s")  # fmt: skip
            assert found.albums
            album = await catalog.album(found.albums[0].ref.id)
            assert album.tracks and all(t.duration_ms > 0 for t in album.tracks)
            print(f"album: {album.kind}, {len(album.tracks)} tracks,"
                  f" {sum(1 for t in album.tracks if t.isrc)} with ISRC")  # fmt: skip
            if album.artist_refs:
                artist_id = album.artist_refs[0].id
                releases = await catalog.artist_releases(artist_id)
                cards = dedupe_editions(releases)
                print(f"artist: {len(releases)} releases, {len(cards)} after de-duplication")
            url = artwork_url(album.artwork_template, 600)
            assert url is not None
            data, content_type = await catalog.artwork(url)
            print(f"artwork: {content_type}, {len(data)} bytes")
            again = time.monotonic()
            await catalog.album(album.ref.id)
            assert time.monotonic() - again < 0.05  # served from the cache
        finally:
            await catalog.aclose()

    anyio.run(main)
