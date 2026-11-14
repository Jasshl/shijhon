"""Where a joined text's list comes from (``artist_lists/sources.py``): the linked catalog
item, the ISRC in the catalogs (every add-on that declares one too), a search there only
when neither gave one; lists that differ are ambiguous; only an add-on's own ``artists``
list counts. The catalogs asked for artist lists only: ``test_artist_list_catalogs.py``."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.artist_lists.sources import Skips, Sources, Subject, Unavailable
from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    release_data,
)
from shijhon.store import Store

pytestmark = pytest.mark.anyio

JOINED = "Ash Vane, Bell Orrin"
SUBJECT = Subject("s1", JOINED, "Night Song", 200_000, ("QZAAA2600001",))


def track(ident: str, *, artists: tuple[str, ...] = (), **extra: Any) -> CatalogTrack:
    return CatalogTrack(
        ref=CatalogRef("cat", ident),
        title=extra.pop("title", "Night Song"),
        artist=extra.pop("artist", JOINED),
        duration_ms=extra.pop("duration_ms", 200_000),
        artists=artists,
        **extra,
    )


class Catalog:
    key = "cat"

    def __init__(self, *, isrc=(), search=(), songs=None, names=None, fail=None) -> None:
        self.isrc, self.found, self.songs = list(isrc), list(search), dict(songs or {})
        self.names = dict(names or {})
        self.fail = fail
        self.asked: list[str] = []

    async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
        self.asked.append("isrc")
        if self.fail in ("isrc", "all"):
            raise CatalogError("unavailable", "down")
        return tuple(self.isrc)

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        self.asked.append("search:" + term)
        if self.fail == "all":
            raise CatalogError("unavailable", "down")
        return SearchResults(songs=tuple(self.found))

    async def song(self, ident: str) -> CatalogTrack:
        self.asked.append("song:" + ident)
        return self.songs[ident]

    async def artist(self, ident: str) -> CatalogArtist:
        self.asked.append("artist:" + ident)
        if ident not in self.names:
            raise CatalogError("not_found", "none")
        return CatalogArtist(CatalogRef("cat", ident), self.names[ident])


class Addons:
    def __init__(self, *catalogs: Catalog) -> None:
        self.list = [(f"add-on {n}", c) for n, c in enumerate(catalogs)]

    async def catalogs(self) -> list[tuple[str, Catalog]]:
        return self.list


@pytest.fixture
async def store(tmp_path: Path):
    opened = await Store.open(tmp_path / "state.db")
    try:
        yield opened
    finally:
        await opened.close()


async def linked(store: Store, linked_track: CatalogTrack, **release: Any) -> None:
    album = CatalogRelease(
        ref=CatalogRef("cat", "al1"),
        title="Night",
        artist=release.pop("artist", JOINED),
        kind=ReleaseKind.ALBUM,
        release_date="2020",
        tracks=(linked_track,),
        **release,
    )
    await store.execute(
        "INSERT INTO releases (ref, folder, album_id, title, artist, album_tags, data,"
        " created_at) VALUES ('cat:al1', 'f', 'a1', 'Night', ?, '{}', ?, 0)",
        [album.artist, json.dumps(release_data(album))],
    )
    await store.execute(
        "INSERT INTO track_links (track_ref, song_id, release_ref, owned)"
        " VALUES (?, 's1', 'cat:al1', 1)",
        [str(linked_track.ref)],
    )


async def test_the_linked_item_s_names_come_first(store: Store) -> None:
    await linked(store, track("t1", artists=("Ash Vane", "Bell Orrin")))
    found = await Sources(store, Catalog(), Addons()).resolve(SUBJECT)
    assert (found.state, found.names, found.source) == (
        "resolved",
        ("Ash Vane", "Bell Orrin"),
        "link",
    )


async def test_the_linked_item_s_artist_items_are_named_by_the_catalog(store: Store) -> None:
    refs = (CatalogRef("cat", "ar1"), CatalogRef("cat", "ar2"))
    await linked(store, track("t1", artist_refs=refs))
    catalog = Catalog(names={"ar1": "Ash Vane", "ar2": "Bell Orrin"})
    found = await Sources(store, catalog, Addons()).resolve(SUBJECT)
    assert found.names == ("Ash Vane", "Bell Orrin") and found.source == "link"
    # A track credited as its album, with none of its own: the album's.
    await store.execute("DELETE FROM track_links")
    await store.execute("DELETE FROM releases")
    await linked(store, track("t1"), artists=("Ash Vane", "Bell Orrin"))
    found = await Sources(store, None, Addons()).resolve(SUBJECT)
    assert found.names == ("Ash Vane", "Bell Orrin")


async def test_the_isrc_is_asked_and_a_search_only_without_a_list(store: Store) -> None:
    by_isrc = Catalog(isrc=[track("i1", artists=("Ash Vane", "Bell Orrin"))])
    found = await Sources(store, None, Addons(by_isrc)).resolve(SUBJECT)
    assert (found.names, found.source) == (("Ash Vane", "Bell Orrin"), "isrc")
    assert by_isrc.asked == ["isrc"]  # (no search)
    searched = Catalog(
        search=[track("f1"), track("f2", title="Another Song")],
        songs={"f1": track("f1", artists=("Ash Vane", "Bell Orrin"))},
    )
    found = await Sources(store, None, Addons(searched)).resolve(SUBJECT)
    assert (found.names, found.source) == (("Ash Vane", "Bell Orrin"), "search")
    # The search's track named no artists: asked for again; the other title is no match.
    assert searched.asked == ["isrc", "search:Night Song Ash Vane", "song:f1"]


async def test_lists_that_differ_are_ambiguous(store: Store) -> None:
    await linked(store, track("t1", artists=("Ash Vane", "Bell Orrin")))
    other = Catalog(isrc=[track("i1", artists=("Ash Vane, Bell Orrin", "Cora"))])
    # (Not the text: it does not count, and so differs from nothing.)
    found = await Sources(store, None, Addons(other)).resolve(SUBJECT)
    assert found.state == "resolved"
    text = Subject("s1", "Ash Vane, Bell Orrin & Cora Dune", isrcs=("QZAAA2600001",))
    await store.execute("DELETE FROM track_links")
    first = Catalog(isrc=[track("i1", artists=("Ash Vane, Bell Orrin", "Cora Dune"))])
    second = Catalog(isrc=[track("i2", artists=("Ash Vane", "Bell Orrin", "Cora Dune"))])
    found = await Sources(store, None, Addons(first, second)).resolve(text)
    assert found.state == "ambiguous" and found.names == ()


async def test_only_an_add_on_s_own_list_counts(store: Store) -> None:
    # A track without an ``artists`` list: its credit is not split for it.
    plain = Catalog(isrc=[track("i1")], search=[track("f1")], songs={"f1": track("f1")})
    found = await Sources(store, None, Addons(plain)).resolve(SUBJECT)
    assert found.state == "unresolved"
    named = Catalog(isrc=[track("i1", artists=("Ash Vane", ""))])  # an unnamed artist
    assert (await Sources(store, None, Addons(named)).resolve(SUBJECT)).state == "unresolved"


async def test_a_failed_add_on_makes_it_a_later_question(store: Store) -> None:
    down = Catalog(fail="isrc")
    with pytest.raises(Unavailable):
        await Sources(store, None, Addons(down)).resolve(SUBJECT)
    # ... also when another gave the list: the one that failed might differ.
    up = Catalog(isrc=[track("i1", artists=("Ash Vane", "Bell Orrin"))])
    with pytest.raises(Unavailable):
        await Sources(store, None, Addons(down, up)).resolve(SUBJECT)
    assert (await Sources(store, None, Addons(up)).resolve(SUBJECT)).state == "resolved"


async def test_a_catalog_that_names_one_artist_of_the_whole_text_vetoes_a_split(
    store: Store,
) -> None:
    band = Catalog(isrc=[track("i1", artists=(JOINED,))])
    split = Catalog(isrc=[track("i2", artists=("Ash Vane", "Bell Orrin"))])
    found = await Sources(store, None, Addons(band, split)).resolve(SUBJECT)
    assert (found.state, found.source) == ("ambiguous", "one_artist")
    assert found.vetoed_by == ("add-on 0",)  # (who said so: the report counts it)
    assert band.asked == ["isrc"]  # (no search either)
    # The linked item's one artist item, named so by the catalog, too.
    await linked(store, track("t1", artist_refs=(CatalogRef("cat", "ar1"),)))
    catalog = Catalog(names={"ar1": JOINED})
    found = await Sources(store, catalog, Addons(split)).resolve(SUBJECT)
    assert found.state == "ambiguous"


async def test_a_featured_artist_the_text_lacks_is_no_match(store: Store) -> None:
    catalog = Catalog(isrc=[replace(track("i1"), artists=("Ash Vane", "Bell Orrin", "Cora"))])
    found = await Sources(store, None, Addons(catalog)).resolve(SUBJECT)
    assert found.state == "unresolved"


async def test_the_linked_item_s_list_counts_while_an_add_on_is_down(store: Store) -> None:
    await linked(store, track("t1", artists=("Ash Vane", "Bell Orrin")))
    down = Catalog(fail="isrc")
    found = await Sources(store, None, Addons(down)).resolve(SUBJECT)
    assert (found.state, found.source) == ("resolved", "link")
    # ... unless an answering source differs, or names one artist of the whole text.
    other = Catalog(isrc=[track("i1", artists=("Ash Vane, Bell", "Orrin"))])
    text = Subject("s1", "Ash Vane, Bell, Orrin", isrcs=("QZAAA2600001",))
    await store.execute("DELETE FROM track_links")
    await store.execute("DELETE FROM releases")
    await linked(store, track("t1", artist="Ash Vane, Bell, Orrin",
                              artists=("Ash Vane", "Bell, Orrin")))  # fmt: skip
    found = await Sources(store, None, Addons(down, other)).resolve(text)
    assert (found.state, found.source) == ("ambiguous", "lists_differ")
    band = Catalog(isrc=[track("i2", artists=("Ash Vane, Bell, Orrin",))])
    found = await Sources(store, None, Addons(down, band)).resolve(text)
    assert (found.state, found.source) == ("ambiguous", "one_artist")


async def test_without_a_link_a_down_add_on_is_a_later_question_that_others_answered(
    store: Store,
) -> None:
    down = Catalog(fail="isrc")
    up = Catalog(isrc=[track("i1", artists=("Ash Vane", "Bell Orrin"))])
    with pytest.raises(Unavailable) as one_down:
        await Sources(store, None, Addons(down, up)).resolve(SUBJECT)
    assert one_down.value.answered  # (not a failure of every catalog)
    with pytest.raises(Unavailable) as all_down:
        await Sources(store, None, Addons(Catalog(fail="all"))).resolve(SUBJECT)
    assert not all_down.value.answered


class Slow(Catalog):
    def __init__(self, seconds: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.seconds = seconds

    async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
        await anyio.sleep(self.seconds)
        return await super().songs_by_isrc(isrc)


async def test_add_ons_are_asked_together_and_read_in_their_order(store: Store) -> None:
    text = Subject("s1", "Ash Vane, Bell Orrin & Cora Dune", isrcs=("QZAAA2600001",))
    slow = Slow(0.3, isrc=[track("i1", artists=("Ash Vane, Bell Orrin", "Cora Dune"))])
    fast = Slow(0.0, isrc=[track("i2", artists=("Ash Vane", "Bell Orrin", "Cora Dune"))])
    started = anyio.current_time()
    lists, missed, answered = await Sources(store, None, Addons(slow, fast)).by_isrc(text)
    assert anyio.current_time() - started < 0.5  # (together, not one after the other)
    # The add-ons' order, whatever answered first: the disagreement is seen either way.
    assert [(asked.name, names) for asked, names in lists] == [
        ("add-on 0", ("Ash Vane, Bell Orrin", "Cora Dune")),
        ("add-on 1", ("Ash Vane", "Bell Orrin", "Cora Dune")),
    ]
    assert (missed, answered) == ([], True)
    found = await Sources(store, None, Addons(slow, Slow(0.0, isrc=fast.isrc))).resolve(text)
    assert (found.state, found.source) == ("ambiguous", "lists_differ")


async def test_an_add_on_that_keeps_failing_is_skipped_for_the_pass(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    dead = Catalog(fail="all")
    up = Catalog(isrc=[track("i1", artists=("Ash Vane", "Bell Orrin"))])
    sources, skips = Sources(store, None, Addons(dead, up)), Skips()
    caplog.set_level("INFO", logger="shijhon.artist_lists.sources")
    # The first song: it failed for this song, not skipped yet: a later question.
    with pytest.raises(Unavailable) as later:
        await sources.resolve(SUBJECT, link=False, skips=skips)
    assert later.value.answered
    # Its second failure skips it for the pass: absent, the other add-on's list counts.
    for _ in range(2):
        found = await sources.resolve(SUBJECT, link=False, skips=skips)
        assert (found.state, found.names) == ("resolved", ("Ash Vane", "Bell Orrin"))
    assert len(dead.asked) == 2  # (the ISRC of the first two songs, no more)
    assert skips.skipped == {"add-on 0": "down"}
    assert [r.message for r in caplog.records].count(
        "artist lists: add-on add-on 0 skipped for this pass: down"
    ) == 1
    # A new pass asks it again.
    with pytest.raises(Unavailable):
        await sources.resolve(SUBJECT, link=False, skips=Skips())
    assert len(dead.asked) == 3


async def test_a_source_skipped_for_the_pass_leaves_the_rules_to_those_that_answered(
    store: Store,
) -> None:
    def skipped() -> Skips:
        skips = Skips()
        for _ in range(2):
            skips.failed("add-on 0", "down")
        return skips

    listed = [track("i1", artists=("Ash Vane", "Bell Orrin"))]
    dead = Catalog(fail="all")
    # Another catalog asked for artist lists only, skipped: the add-on's list counts.
    other = Catalog(fail="all")
    skips = Skips()
    for _ in range(2):
        skips.failed("the extra catalog", "503", addon=False)
    found = await Sources(store, None, Addons(Catalog(isrc=listed)), [("extra", other)]).resolve(
        SUBJECT, link=False, skips=skips
    )
    assert (found.state, found.source) == ("resolved", "isrc") and other.asked == []
    # Lists that differ among those that answered: still ambiguous.
    differs = [track("i2", artists=("Ash Vane, Bell Orrin",)), *listed]
    found = await Sources(store, None, Addons(dead, Catalog(isrc=differs))).resolve(
        SUBJECT, link=False, skips=skipped()
    )
    assert found.state == "ambiguous" and dead.asked == []
    # No list among those that answered: the skipped one might have one, a later question.
    with pytest.raises(Unavailable) as later:
        await Sources(store, None, Addons(dead, Catalog())).resolve(
            SUBJECT, link=False, skips=skipped()
        )
    assert later.value.answered
    # A song no catalog could be asked about (no ISRC, no title): nothing a skipped one
    # would have been asked either.
    found = await Sources(store, None, Addons(dead, Catalog())).resolve(
        Subject("s2", JOINED), link=False, skips=skipped()
    )
    assert found.state == "unresolved"


async def test_a_catalog_skipped_for_the_pass_is_not_asked_for_linked_artist_items(
    store: Store,
) -> None:
    from shijhon.artist_lists.sources import MAIN

    class Down(Catalog):
        async def artist(self, ident: str) -> CatalogArtist:
            self.asked.append("artist:" + ident)
            raise CatalogError("unavailable", "down")

    refs = (CatalogRef("cat", "ar1"), CatalogRef("cat", "ar2"))
    await linked(store, track("t1", artist_refs=refs))
    up = Catalog(isrc=[track("i1", artists=("Ash Vane", "Bell Orrin"))])
    # Its linked artist items failing for the second time: skipped, absent from then on.
    main, skips = Down(fail="all"), Skips()
    skips.failed(MAIN, "down", addon=False)
    found = await Sources(store, main, Addons(up)).resolve(SUBJECT, skips=skips)
    assert main.asked[0] == "artist:ar1" and skips.skips(MAIN)
    assert (found.state, found.source) == ("resolved", "isrc")
    # Skipped already: not asked, the add-on's list counts.
    main.asked.clear()
    found = await Sources(store, main, Addons(up)).resolve(SUBJECT, skips=skips)
    assert main.asked == [] and (found.state, found.source) == ("resolved", "isrc")
    # Its first failure, not skipped: a later question, as before.
    with pytest.raises(Unavailable):
        await Sources(store, Down(fail="all"), Addons(up)).resolve(SUBJECT, skips=Skips())


async def test_a_catalog_that_raises_something_unexpected_counts_as_failed(
    store: Store,
) -> None:
    class Broken(Catalog):
        async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
            raise ValueError("unreadable answer")

    up = Catalog(isrc=[track("i1", artists=("Ash Vane", "Bell Orrin"))])
    sources, skips = Sources(store, None, Addons(Broken(), up)), Skips()
    with pytest.raises(Unavailable) as later:
        await sources.resolve(SUBJECT, link=False, skips=skips)
    assert later.value.answered and skips.failures["add-on 0"] == 1


def test_every_add_on_that_declares_a_catalog_is_asked() -> None:
    """(``AddonCatalogs.catalogs()``: an add-on that also streams is asked too.)"""
    import httpx

    from shijhon.catalog.addon import AddonCatalogs
    from shijhon.delivery.addon import Addon
    from tests.harness.addon_catalog import FakeCatalog
    from tests.unit.test_addon_catalog import BASE, Listed, Registry, Server

    streaming = Server(FakeCatalog())  # ("stream", "isrc", "search", "catalog")
    catalog_only = Server(FakeCatalog(), ("search", "catalog", "isrc"))
    catalog_only.service = "test.other"

    def client(server: Server) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(server.handle))

    registry = Registry(
        [
            Listed(1, "Streams", Addon(BASE, None, client(streaming))),
            Listed(2, "Catalog", Addon(BASE, None, client(catalog_only))),
        ]
    )

    async def run() -> None:
        catalogs = AddonCatalogs(registry)  # type: ignore[arg-type]
        try:
            # (Sources asks each catalog listed here: the streaming add-on's list counts.)
            assert [n for n, _ in await catalogs.catalogs()] == ["Streams", "Catalog"]
        finally:
            await catalogs.aclose()

    anyio.run(run)


async def test_the_catalog_itself_is_asked_by_isrc_and_search_first(store: Store) -> None:
    by_isrc = Catalog(isrc=[track("c1", artists=("Ash Vane", "Bell Orrin"))])
    found = await Sources(store, by_isrc, Addons()).resolve(SUBJECT)
    assert (found.state, found.names, found.source) == (
        "resolved",
        ("Ash Vane", "Bell Orrin"),
        "catalog_isrc",
    )
    # Its search names no artists: each candidate is asked for again.
    searched = Catalog(
        search=[track("c2")], songs={"c2": track("c2", artists=("Ash Vane", "Bell Orrin"))}
    )
    found = await Sources(store, searched, Addons()).resolve(SUBJECT)
    assert found.source == "catalog_search" and "song:c2" in searched.asked
    # It and an add-on that differ: ambiguous (every answering source is seen).
    text = Subject("s1", "Ash Vane, Bell Orrin & Cora Dune", isrcs=("QZAAA2600001",))
    main = Catalog(isrc=[track("c3", artists=("Ash Vane, Bell Orrin", "Cora Dune"))])
    addon = Catalog(isrc=[track("i3", artists=("Ash Vane", "Bell Orrin", "Cora Dune"))])
    found = await Sources(store, main, Addons(addon)).resolve(text)
    assert (found.state, found.source) == ("ambiguous", "lists_differ")


async def test_the_catalog_s_own_list_counts_while_an_add_on_is_down(store: Store) -> None:
    main = Catalog(isrc=[track("c1", artists=("Ash Vane", "Bell Orrin"))])
    found = await Sources(store, main, Addons(Catalog(fail="all"))).resolve(SUBJECT)
    assert (found.state, found.source) == ("resolved", "catalog_isrc")
    # The catalog down: an add-on's list waits for it.
    up = Catalog(isrc=[track("i1", artists=("Ash Vane", "Bell Orrin"))])
    with pytest.raises(Unavailable) as later:
        await Sources(store, Catalog(fail="all"), Addons(up)).resolve(SUBJECT)
    assert later.value.answered
    # ... but the linked item's list counts.
    await linked(store, track("t1", artists=("Ash Vane", "Bell Orrin")))
    found = await Sources(store, Catalog(fail="all"), Addons(up)).resolve(SUBJECT)
    assert (found.state, found.source) == ("resolved", "link")
