"""The demo catalog (``shijhon_demo_catalog``: invented data in Shijhon's own
model) and the tests' replay of it, plus the shared TTL cache, request coalescing and
edition de-duplication. The records keep the variants a real catalog has;
what a real adapter does with its catalog's answers is tested with that adapter."""

from __future__ import annotations

import logging
import struct
import zlib
from dataclasses import replace

import anyio
import pytest
from pydantic import SecretStr

from shijhon.catalog import plugin
from shijhon.catalog.base import CatalogError, SearchResults, scope
from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.editions import dedupe_editions, edition_key
from shijhon.catalog.model import (
    ITEM_ID,
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    artwork_url,
    release_data,
    release_from_data,
    track_data,
    track_from_data,
)
from shijhon.catalog.setup import build_catalog
from shijhon.config import CatalogSettings, catalog_settings, load_settings
from shijhon.views.shown import ShownArtists
from shijhon_demo_catalog import KEY, NOTICE, DemoCatalog, load_records
from tests.harness.library import cover_image
from tests.harness.replay import REGION, Replay, fixture

pytestmark = pytest.mark.anyio


def album_id(name: str) -> str:
    return str(fixture(name)["path"]).split("/")[1]


def song_id(name: str) -> str:
    return str(fixture(name)["path"]).split("/")[1]


def search_term(name: str) -> str:
    return str(fixture(name)["params"]["term"])


async def test_album_with_tracks_isrc_and_positions() -> None:
    replay = Replay()
    demo = replay.catalog()
    release = await demo.album(album_id("album-anniversary-super-deluxe"))
    assert release.ref == CatalogRef("demo", album_id("album-anniversary-super-deluxe"))
    assert release.kind == ReleaseKind.ALBUM and release.explicit and not release.clean
    assert len(release.tracks) == release.track_count == 75
    assert all(t.isrc and t.isrc.startswith("ZZSHJ") for t in release.tracks)
    assert {t.disc for t in release.tracks} >= {1, 2}
    assert all(t.album == release.ref and t.duration_ms > 0 for t in release.tracks)
    assert release.artist_refs and release.release_date and release.upc
    assert release.artwork_template and "{w}" in release.artwork_template
    assert replay.log == [f"albums/{release.ref.id}"]
    await demo.aclose()


async def test_release_kinds_and_clean_twins() -> None:
    demo = Replay().catalog()
    single = await demo.album(album_id("album-single-radio-edit"))
    assert single.kind == ReleaseKind.SINGLE and single.title.endswith(" - Single")
    # A catalog does not mark every EP: this one is a plain album.
    assert (await demo.album(album_id("album-ep-without-suffix"))).kind == ReleaseKind.ALBUM
    compilation = await demo.album(album_id("album-soundtrack-compilation"))
    assert compilation.kind == ReleaseKind.COMPILATION
    assert len({t.artist for t in compilation.tracks}) > 1
    explicit = await demo.album(album_id("album-twins-explicit"))
    clean = await demo.album(album_id("album-twins-clean"))
    assert explicit.title == clean.title and explicit.explicit and clean.clean
    # The clean twin spells some credits differently ("(FEAT. NAME.)").
    assert [t.title.casefold() for t in explicit.tracks] == [
        t.title.casefold().replace(".)", ")") for t in clean.tracks
    ]
    assert [t.isrc for t in explicit.tracks] != [t.isrc for t in clean.tracks]


async def test_a_song_knows_its_album_and_every_album_track_is_a_song() -> None:
    replay = Replay()
    demo = replay.catalog()
    song = await demo.song(song_id("song-twins-other-edition"))
    assert song.album is not None and song.album.id != album_id("album-twins-edition-clean")
    assert song.explicit and song.isrc and song.artist_refs
    feat = await demo.song(song_id("song-duo-feat"))
    assert "(feat. " in feat.title and " & " in feat.artist
    # A track of a recorded album, by its ID and by its ISRC: with its album and the
    # album's artist items.
    album = await demo.album(album_id("album-twins-clean"))
    track = album.tracks[2]
    assert not track.artist_refs  # an album's track list names none
    found = await demo.song(track.ref.id)
    assert found.title == track.title and found.album == album.ref
    assert found.artist_refs == album.artist_refs
    assert track.isrc is not None
    by_isrc = await demo.songs_by_isrc(track.isrc.lower())
    assert found in by_isrc and all(s.isrc == track.isrc for s in by_isrc)
    assert await demo.songs_by_isrc("ZZZZZ0000000") == ()
    assert replay.log[-2:] == [f"songs?isrc={track.isrc.lower()}", "songs?isrc=ZZZZZ0000000"]


async def test_search_finds_artists_albums_and_songs_with_their_albums() -> None:
    replay = Replay()
    demo = replay.catalog()
    term = search_term("search-single-vs-album")
    results = await demo.search(term)
    assert results.artists and results.albums and results.songs
    kinds = {a.kind for a in results.albums}
    assert ReleaseKind.SINGLE in kinds and ReleaseKind.ALBUM in kinds
    assert all(s.album is not None for s in results.songs)
    # Search answers name no artist items (as a real catalog's may not): asked for by ID.
    assert not any(a.artist_refs for a in results.albums)
    assert not any(s.artist_refs for s in results.songs)
    songs = tuple(s.ref.id for s in results.songs)
    albums = tuple(a.ref.id for a in results.albums)
    credits = await demo.artists_of(songs, albums)
    assert credits and set(credits) <= {f"songs:{i}" for i in songs} | {
        f"albums:{i}" for i in albums
    }
    assert all(ref.catalog == KEY for refs in credits.values() for ref in refs)
    assert replay.log == [f"search?term={term}", "songs", "albums"]
    assert await demo.artists_of((), ()) == {} and len(replay.log) == 3  # nothing to ask
    few = await demo.search(term.upper(), 2)  # any letter case; at most the limit of each
    assert (len(few.artists), len(few.albums), len(few.songs)) == (
        min(2, len(results.artists)),
        2,
        2,
    )


async def test_artist_releases_and_top_songs() -> None:
    replay = Replay()
    demo = replay.catalog()
    artist_id = album_id("artist-duo")
    artist = await demo.artist(artist_id)
    assert artist.name and artist.artwork_template
    releases = await demo.artist_releases(artist_id)
    kinds = {r.kind for r in releases}
    assert ReleaseKind.ALBUM in kinds and ReleaseKind.SINGLE in kinds
    band = album_id("artist-band")
    assert await demo.artist_releases(band)  # an artist without singles
    assert len(await demo.artist_releases(album_id("artist-composer"))) >= 100
    assert await demo.artist_releases("900009999") == ()  # an artist without releases here
    top = fixture("top-songs-band")
    songs = await demo.top_songs(band, 5)
    assert [s.ref.id for s in songs] == [row["ref"]["id"] for row in top["body"][:5]]
    assert all(s.album is not None and s.duration_ms > 0 and s.isrc for s in songs)
    assert await demo.top_songs("900009999") == ()  # an artist without any
    assert replay.log[-2:] == [f"artists/{band}/top-songs", "artists/900009999/top-songs"]
    assert [track_from_data(track_data(s)) for s in songs] == list(songs)  # saved, read back


async def test_the_replay_fails_delays_and_scripts_requests() -> None:
    """What the suites inject around the records: a failure by its HTTP status (as an HTTP
    catalog would report it; the reason never holds a URL), a delay for one request or
    all, a search of the test's own answered with a recorded one, albums hidden from search
    answers, a changed record, and every request logged with its time."""
    replay = Replay()
    demo = replay.catalog()
    with pytest.raises(CatalogError) as missing:
        await demo.album("1")  # a recorded "not found"
    assert missing.value.kind == "not_found"
    with pytest.raises(CatalogError) as unknown:
        await demo.album("../../me")
    assert unknown.value.kind == "not_found"
    wanted = album_id("album-twins-clean")
    kinds = {429: "rate_limited", 401: "unauthorized", 403: "unauthorized", 404: "not_found",
             500: "unavailable", 503: "unavailable"}  # fmt: skip
    for status, kind in kinds.items():
        replay.failing[f"albums/{wanted}"] = status
        with pytest.raises(CatalogError) as failed:
            await demo.album(wanted)
        assert failed.value.kind == kind and "://" not in str(failed.value)
    replay.failing.clear()
    for key, call in (
        ("search", lambda: demo.search("anything")),
        ("songs", lambda: demo.songs_by_isrc("ZZSHJ0000146")),
        ("songs", lambda: demo.artists_of(("1",), ())),
        ("albums", lambda: demo.artists_of((), ("1",))),
        (f"artists/{wanted}", lambda: demo.artist(wanted)),
        (f"artists/{wanted}/releases", lambda: demo.artist_releases(wanted)),
        (f"artists/{wanted}/top-songs", lambda: demo.top_songs(wanted)),
        (f"songs/{wanted}", lambda: demo.song(wanted)),
    ):
        replay.failing[key] = 503
        with pytest.raises(CatalogError) as failed:
            await call()
        assert failed.value.kind == "unavailable", key
        del replay.failing[key]
    # Slow: one request, or every one.
    replay.slow[f"albums/{wanted}"] = 0.3
    started = anyio.current_time()
    await demo.album(wanted)
    assert 0.25 < anyio.current_time() - started < 2.0
    replay.slow.clear()
    slow = Replay(delay=0.2)
    started = anyio.current_time()
    await slow.catalog().album(wanted)
    assert anyio.current_time() - started > 0.15
    # Searches: only recorded terms answer; an alias answers a term of the test's own.
    term = search_term("search-editions")
    with pytest.raises(CatalogError) as nothing:
        await demo.search("northern letters")
    assert nothing.value.kind == "not_found"
    replay.aliases["northern letters"] = term
    found = await demo.search("Northern Letters")
    assert found == await demo.search(term) and found.albums
    replay.hidden = {found.albums[0].ref.id}
    assert [a.ref.id for a in (await demo.search(term)).albums] == [
        a.ref.id for a in found.albums[1:]
    ]
    assert replay.searches[term]["body"]["albums"][0]["ref"]["id"] in replay.hidden  # untouched
    # A changed record is the next answer; another replay has its own records.
    replay.by_path[f"albums/{wanted}"]["body"]["title"] = "Renamed"
    assert (await demo.album(wanted)).title == "Renamed"
    assert (await replay.catalog().album(wanted)).title == "Renamed"  # the replay's records
    assert (await Replay().catalog().album(wanted)).title != "Renamed"
    assert replay.api_requests == len(replay.log) == len(replay.times)
    assert replay.times == sorted(replay.times)
    assert replay.log[0] == "albums/1" and f"search?term={term}" in replay.log
    # Covers: the address is checked, the request counted, the harness's image answered.
    template = (await demo.album(wanted)).artwork_template
    url = artwork_url(template, 300)
    assert url is not None and url.startswith("https://covers.demo.invalid/")
    assert await demo.artwork(url) == (cover_image("blue").read_bytes(), "image/jpeg")
    assert (replay.artwork_requests, replay.artwork_urls) == (1, [url])
    with pytest.raises(CatalogError) as refused:
        await demo.artwork("https://images.example.invalid/1/300x300.png")
    assert refused.value.kind == "invalid" and replay.artwork_requests == 1
    assert (demo.key, demo.region, scope(demo)) == ("demo", REGION, "demo.xx")


async def test_the_demo_catalog_is_installed_and_says_it_is_invented(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``kind = "demo"``: found through its package's entry point (the development
    environment has it), without settings of its own, and labeled as invented wherever it
    is named."""
    adapter = plugin.adapter("demo")
    assert "invented" in adapter.label and "invented" in adapter.notice == NOTICE
    assert catalog_settings("demo") is CatalogSettings
    settings = load_settings(None, catalog={"kind": "demo"})
    with caplog.at_level(logging.WARNING, logger="shijhon.catalog.setup"):
        demo = build_catalog(settings.catalog)
    assert isinstance(demo, DemoCatalog) and "invented" in caplog.text
    assert (demo.key, demo.region, scope(demo)) == ("demo", "", "demo")
    await demo.check()
    # Its records name no provider, and every ID fits Shijhon's IDs.
    text = repr(load_records()).lower()
    assert KEY in text and "https://covers.demo.invalid/" in text
    album = await demo.album(album_id("album-twins-clean"))
    assert all(ITEM_ID.fullmatch(t.ref.id) for t in album.tracks)
    assert release_from_data(release_data(album)) == album


async def test_the_demo_finds_items_by_their_names() -> None:
    """A newcomer searches for what the demo holds: a term that is not a recorded one finds
    the artists, albums and songs whose names hold its words."""
    demo = DemoCatalog()
    recorded = await demo.search(search_term("search-editions"))
    assert recorded.albums and recorded.songs  # a recorded term: its recorded answer
    found = await demo.search("brokenmeadow")
    assert [a.name for a in found.artists] == ["BrokenMeadow"]
    assert found.albums and all("BrokenMeadow" in a.artist for a in found.albums)
    assert found.songs and not any(a.tracks for a in found.albums)
    title = (await demo.album(album_id("album-twins-clean"))).title
    words = await demo.search(f"  {title.upper()}  ")
    assert title in {a.title for a in words.albums}
    assert len((await demo.search("a", 3)).songs) == 3  # at most the limit
    assert await demo.search("zzzz-nothing-like-this") == SearchResults()
    assert await demo.search("   ") == SearchResults()


async def test_the_demo_s_covers_are_squares_made_here() -> None:
    demo = DemoCatalog()
    album = await demo.album(album_id("album-twins-clean"))
    data, content_type = await demo.artwork(artwork_url(album.artwork_template, 300) or "")
    assert content_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (300, 300)
    rows = zlib.decompress(data[data.index(b"IDAT") + 4 : data.index(b"IEND") - 8])
    assert len(rows) == 300 * (1 + 3 * 300) and len(set(rows[1:901:3])) == 1  # one color
    again, _ = await demo.artwork(artwork_url(album.artwork_template, 300) or "")
    assert again == data  # the same cover every time
    other = await demo.album(album_id("album-feat-standard"))
    assert (await demo.artwork(artwork_url(other.artwork_template, 300) or ""))[0] != data
    huge, _ = await demo.artwork("https://covers.demo.invalid/1/9999x9999.png")
    assert struct.unpack(">II", huge[16:24]) == (1200, 1200)  # never larger
    for url in (
        "http://covers.demo.invalid/1/300x300.png",
        "https://covers.demo.invalid.example.invalid/1/300x300.png",
        "https://covers.demo.invalid/1/300x300.png?x=1",
        "https://covers.demo.invalid/a/b/300x300.png",
    ):
        with pytest.raises(CatalogError):
            await demo.artwork(url)


async def test_cache_coalesces_concurrent_requests_and_keeps_answers() -> None:
    replay = Replay(delay=0.05)
    cached = CachedCatalog(replay.catalog(), ttl=60)
    wanted = album_id("album-feat-standard")
    results = []

    async def fetch() -> None:
        results.append(await cached.album(wanted))

    async with anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(fetch)
    await cached.album(wanted)
    assert len(results) == 5 and replay.api_requests == 1
    await cached.aclose()


async def test_cache_does_not_keep_failures() -> None:
    replay = Replay()
    cached = CachedCatalog(replay.catalog(), ttl=60)
    wanted = album_id("album-twins-clean")
    replay.failing[f"albums/{wanted}"] = 503
    with pytest.raises(CatalogError):
        await cached.album(wanted)
    del replay.failing[f"albums/{wanted}"]
    assert (await cached.album(wanted)).title
    await cached.aclose()


async def test_a_song_shown_by_an_answer_stands_in_while_the_catalog_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A play does not depend on its song's lookup succeeding. A song
    an answer (an album, a search) showed is used as shown while its lookup fails or keeps
    it waiting; "not found" stays not found; a song never shown fails as before."""
    replay = Replay()
    cached = CachedCatalog(replay.catalog(), ttl=60)
    album = await cached.album(album_id("album-twins-clean"))
    first, second, third = album.tracks[:3]
    replay.failing[f"songs/{first.ref.id}"] = 503
    assert (await cached.song(first.ref.id)).title == first.title  # remembered
    replay.failing[f"songs/{second.ref.id}"] = 404
    with pytest.raises(CatalogError) as missing:
        await cached.song(second.ref.id)
    assert missing.value.kind == "not_found"
    replay.failing["songs/1"] = 503
    with pytest.raises(CatalogError) as failed:
        await cached.song("1")  # never shown
    assert failed.value.kind == "unavailable"
    monkeypatch.setattr("shijhon.catalog.cache.SHOWN_WAIT", 0.2)
    replay.slow[f"songs/{third.ref.id}"] = 2.0
    started = anyio.current_time()
    assert (await cached.song(third.ref.id)).title == third.title
    assert anyio.current_time() - started < 1.0  # not the slow lookup's 2 s
    await cached.aclose()


async def test_remembered_songs_are_bounded_and_can_be_off() -> None:
    replay = Replay()
    few = CachedCatalog(replay.catalog(), ttl=60, remember=2)
    album = await few.album(album_id("album-twins-clean"))
    kept = [t.ref.id for t in album.tracks[-2:]]
    assert list(few._shown) == kept
    none = CachedCatalog(replay.catalog(), ttl=60, remember=0)
    await none.album(album_id("album-twins-clean"))
    assert not none._shown
    await few.aclose()
    await none.aclose()


async def test_editions_one_card_preferring_explicit_then_a_slightly_fuller_edition() -> None:
    demo = Replay().catalog()
    names = [
        "album-twins-clean",
        "album-twins-explicit",
        "album-anniversary-standard",
        "album-anniversary-super-deluxe",
        "album-duo-deluxe-clean",
        "album-duo-deluxe-explicit",
        "album-twins-edition-clean",
    ]
    names += ["album-editions-remastered", "album-editions-super-deluxe", "album-editions-mix"]
    releases = [await demo.album(album_id(n)) for n in names]
    chosen = {r.ref.id for r in dedupe_editions(releases)}
    assert chosen == {
        album_id("album-twins-explicit"),
        # 13 tracks and a 75-track super deluxe: two cards.
        album_id("album-anniversary-standard"),
        album_id("album-anniversary-super-deluxe"),
        album_id("album-duo-deluxe-explicit"),
        album_id("album-twins-edition-clean"),  # a differently named edition stays
        # Remastered (18 tracks) and a 40-track super deluxe: two cards.
        album_id("album-editions-remastered"),
        album_id("album-editions-super-deluxe"),
        album_id("album-editions-mix"),  # "(2019 Mix)" is not an edition marker
    }
    await demo.aclose()


async def test_singles_keep_their_names_in_de_duplication() -> None:
    demo = Replay().catalog()
    artist_id = str(fixture("artist-duo")["path"]).split("/")[1]
    releases = await demo.artist_releases(artist_id)
    singles = [r for r in releases if r.kind == ReleaseKind.SINGLE]
    kept = [r for r in dedupe_editions(releases) if r.kind == ReleaseKind.SINGLE]
    assert len({edition_key(r) for r in singles}) == len(kept)
    assert len(kept) > 1
    await demo.aclose()


def test_artwork_url_from_template() -> None:
    """A template's ``{w}`` and ``{h}`` are the size; anything else is its adapter's."""
    assert artwork_url("https://covers.example.invalid/a/{w}x{h}.jpg", 600) == (
        "https://covers.example.invalid/a/600x600.jpg"
    )
    assert artwork_url("https://covers.example.invalid/a.jpg", 600) == (
        "https://covers.example.invalid/a.jpg"
    )
    assert artwork_url(None, 600) is None


def test_same_titled_albums_from_other_years_stay_separate() -> None:
    def album(ref: str, year: str, tracks: int) -> CatalogRelease:
        return CatalogRelease(
            CatalogRef("demo", ref),
            "Quiet Engines (Artist's Version)" if ref == "b" else "Quiet Engines",
            "Mara Vance",
            ReleaseKind.ALBUM,
            year,
            track_count=tracks,
        )

    chosen = dedupe_editions([album("a", "2008-11-11", 13), album("b", "2021-11-12", 26)])
    assert [r.ref.id for r in chosen] == ["a", "b"]
    same = dedupe_editions([album("a", "2008-11-11", 13), album("c", "2008-11-11", 16)])
    assert [r.ref.id for r in same] == ["c"]


def release(
    ref: str, title: str, date: str, tracks: int, *, explicit: bool = False
) -> CatalogRelease:
    return CatalogRelease(
        CatalogRef("demo", ref),
        title,
        "Mara Vance",
        ReleaseKind.ALBUM,
        date,
        explicit=explicit,
        track_count=tracks,
    )


def cards(*releases: CatalogRelease) -> list[str]:
    return [r.ref.id for r in dedupe_editions(releases)]


def test_identical_titles_merge_only_within_the_same_year() -> None:
    """A self-titled series stays separate; twins of one year merge."""
    assert cards(
        release("a", "Mara Vance", "2001-05-01", 10),
        release("b", "Mara Vance", "2004-05-01", 11),
        release("c", "Mara Vance", "2004-05-01", 11, explicit=True),
    ) == ["a", "c"]


def test_an_edition_marker_merges_whatever_the_year() -> None:
    assert cards(
        release("a", "Quiet Engines", "1994-05-01", 12),
        release("b", "Quiet Engines (Remastered 2015)", "2015-02-01", 12),
        release("c", "Quiet Engines - 20th Anniversary Edition", "2014-06-01", 15),
    ) == ["c"]
    # With a self-titled series, an edition joins the album of its year, else the latest
    # earlier one.
    assert cards(
        release("s1", "Mara Vance", "2001-05-01", 10),
        release("s2", "Mara Vance", "2004-05-01", 11),
        release("dx", "Mara Vance (Deluxe Edition)", "2006-05-01", 13),
    ) == ["s1", "dx"]


def test_unknown_suffixes_stay_separate() -> None:
    assert cards(
        release("a", "Quiet Engines", "2008-05-01", 12),
        release("b", "Quiet Engines (Artist's Version)", "2008-05-01", 12),
        release("c", "Quiet Engines (2019 Mix)", "2008-05-01", 12),
        release("d", "Quiet Engines (Live)", "2008-05-01", 12),
    ) == ["a", "b", "c", "d"]


def test_much_larger_editions_keep_a_card_of_their_own() -> None:
    """The standard edition, or a deluxe with a few more tracks, represents the album; an
    edition with more than about 1.5 times its tracks is a card of its own."""
    assert cards(
        release("std", "Salt Harbor", "1984-05-01", 12),
        release("dlx", "Salt Harbor (Deluxe)", "1984-05-01", 16),
        release("big", "Salt Harbor (Super Deluxe Edition)", "1984-05-01", 40),
        release("big2", "Salt Harbor (Super Deluxe Edition)", "1984-05-01", 40, explicit=True),
    ) == ["dlx", "big2"]
    # Exactly 1.5 times still merges.
    assert cards(
        release("std", "Salt Harbor", "1984-05-01", 12),
        release("dlx", "Salt Harbor (Deluxe)", "1984-05-01", 18),
    ) == ["dlx"]


async def test_top_songs_are_cached() -> None:
    replay = Replay()
    cache = CachedCatalog(replay.catalog(), ttl=60)
    band = str(fixture("artist-band")["path"]).split("/")[1]
    songs = await cache.top_songs(band)
    assert songs and (await cache.top_songs(band)) == songs
    assert replay.log.count(f"artists/{band}/top-songs") == 1
    await cache.aclose()


def test_the_artists_shown_by_name() -> None:
    """The first of a name in one answer (its best match), the most recent answer last."""
    shown = ShownArtists(size=3)
    first, second = CatalogRef("demo", "1"), CatalogRef("demo", "2")
    shown.shown([CatalogArtist(first, "Mara Vance"), CatalogArtist(second, "MARA VANCE")])
    assert shown.find("mara vance") == first and shown.name_of(second) == "MARA VANCE"
    shown.shown([CatalogArtist(second, "Mara Vance")])  # a later answer
    assert shown.find("Mara Vance") == second
    for n in range(3):
        shown.shown([CatalogArtist(CatalogRef("demo", f"x{n}"), f"Other {n}")])
    assert shown.find("Mara Vance") is None  # bounded
    assert shown.find("") is None


# --- the adapter contract kit (shijhon.catalog.contract) ---------------------------------


async def test_the_demo_and_the_sample_adapter_keep_the_contract() -> None:
    """The checks any adapter can run: the tests' own catalogs pass them."""
    from shijhon.catalog import contract
    from tests.harness.sample_adapter import KIND, SAMPLE

    contract.check_declaration("demo", plugin.adapter("demo"))
    contract.check_declaration(KIND, SAMPLE)
    term = search_term("search-single-vs-album")
    await contract.check_catalog(DemoCatalog(), contract.Sample(search=term))
    await contract.check_catalog(DemoCatalog(region="xx"), contract.Sample(search="brokenmeadow"))
    replay = Replay()
    sample = contract.Sample(search=term, album=album_id("album-feat-standard"), limit=3)
    await contract.check_catalog(replay.catalog(), sample)
    assert replay.artwork_requests == 1 and "check" in replay.log


async def test_the_contract_kit_says_what_does_not_hold() -> None:
    """A catalog that breaks what Shijhon builds on is told so, everything at once."""
    from pydantic import BaseModel

    from shijhon.catalog import contract
    from shijhon.catalog.plugin import Adapter, Problem

    class Broken(DemoCatalog):
        key = "Not-A-Key"

    with pytest.raises(contract.ContractError, match="key: lower-case letters and digits"):
        await contract.check_catalog(Broken(), contract.Sample(search="brokenmeadow"))

    class Sloppy(DemoCatalog):
        async def search(self, term: str, limit: int = 20) -> SearchResults:
            found = await DemoCatalog.search(self, term, 25)  # more than asked for
            return found

        async def album(self, album_id: str) -> CatalogRelease:
            release = await DemoCatalog.album(self, album_id)
            other = CatalogRef("elsewhere", "has spaces")
            art = "https://covers.demo.invalid/1/{w}x{h}{c}.png"
            return replace(release, ref=other, release_date="next year", artwork_template=art)

        async def song(self, song_id: str) -> CatalogTrack:
            if song_id == "0":
                raise CatalogError("unavailable", "see https://catalog.example.invalid/x")
            return replace(await DemoCatalog.song(self, song_id), duration_ms=0, isrc="lower")

        async def top_songs(self, artist_id: str, limit: int = 10) -> tuple[CatalogTrack, ...]:
            return await DemoCatalog.top_songs(self, artist_id, 10)

        async def artwork(self, url: str) -> tuple[bytes, str]:
            return b"anything", "text/html"  # whatever address it is given

    with pytest.raises(contract.ContractError) as broken:
        term = search_term("search-anniversary")
        await contract.check_catalog(Sloppy(), contract.Sample(search=term, limit=2))
    text = str(broken.value)
    for problem in (
        "search: more songs than the limit",
        "of catalog 'elsewhere', not 'demo'",
        "an ID Shijhon cannot carry",
        "release date not YYYY",
        "artwork has placeholders besides {w} and {h}",
        "album: another item than the one asked for",
        "no duration",
        "ISRC not 12 upper-case letters and digits",
        "top_songs: more than the limit",
        "artwork: the content type is not an image's",
        "artwork: not a JPEG, PNG, GIF or WebP image by its bytes",
        "artwork: fetched an address that is not the catalog's",
        "song: unavailable for an unknown ID, not not_found",
        "song: an address in the error's reason",
    ):
        assert problem in text, problem

    class Credits(DemoCatalog):
        """A catalog that names no artist items at all (display credits only)."""

        async def album(self, album_id: str) -> CatalogRelease:
            return replace(await DemoCatalog.album(self, album_id), artist_refs=())

        async def artists_of(
            self, songs: tuple[str, ...], albums: tuple[str, ...]
        ) -> dict[str, tuple[CatalogRef, ...]]:
            return {}

        async def search(self, term: str, limit: int = 20) -> SearchResults:
            found = await DemoCatalog.search(self, term, limit)
            return SearchResults((), found.albums, found.songs)

    await contract.check_catalog(Credits(), contract.Sample(search="brokenmeadow"))

    class Mislabeled(DemoCatalog):
        """The image's type is read from its bytes: what the catalog calls it is not held
        against it (Shijhon never uses the claim)."""

        async def artwork(self, url: str) -> tuple[bytes, str]:
            data, _ = await DemoCatalog.artwork(self, url)
            return data, "image/jpeg"  # (a PNG)

    await contract.check_catalog(Mislabeled(), contract.Sample(search="brokenmeadow"))

    class Timed(BaseModel):
        token: SecretStr | None = None

    def reads_the_section(settings: object) -> Problem | None:
        assert settings.kind == "timed" and settings.timeout_seconds == 15  # type: ignore[attr-defined]
        return None if settings.token else Problem("token", "Set it.", "is needed.")  # type: ignore[attr-defined]

    timed = Adapter(label="Timed", build=Broken, settings=Timed, problem=reads_the_section)
    contract.check_declaration("timed", timed)  # problem() is given the whole section
    bare = Adapter(label="Bare", build=Broken, problem=lambda settings: None)
    contract.check_declaration("bare", bare)

    class Careless(BaseModel):
        api_key: str = ""  # a secret by its name, kept as plain text

    def raising(settings: object) -> Problem | None:
        raise RuntimeError("no")

    careless = Adapter(label=" ", build=Broken, settings=Careless, problem=raising)
    with pytest.raises(contract.ContractError) as declared:
        contract.check_declaration("careless", careless)
    text = str(declared.value)
    assert "api_key: named like a secret" in text and "label: empty" in text
    assert "problem() raised RuntimeError" in text
    with pytest.raises(contract.ContractError, match="lower-case letters"):
        contract.check_declaration("Bad Name", careless)


async def test_the_demo_opens_every_artist_and_album_it_shows() -> None:
    """Nothing the demo lists leads to "not found": every album of a search or an artist's
    releases opens with its tracks, every song's album does, and every artist item named
    anywhere - also one without a record of its own - opens with its releases."""
    demo = DemoCatalog()
    artists: set[str] = set()
    albums: set[str] = set()
    for term in ("a", "e", "o", search_term("search-editions"), search_term("search-covers")):
        found = await demo.search(term, 25)
        artists |= {a.ref.id for a in found.artists}
        albums |= {a.ref.id for a in found.albums}
        albums |= {s.album.id for s in found.songs if s.album is not None}
    assert len(albums) > 10 and artists
    for album_ref in sorted(albums):
        album = await demo.album(album_ref)
        assert album.tracks
        artists |= {ref.id for ref in album.artist_refs}
        artists |= {ref.id for t in album.tracks for ref in (await demo.song(t.ref.id)).artist_refs}
    assert len(artists) > 5
    for artist_id in sorted(artists):
        artist = await demo.artist(artist_id)
        assert artist.name and artist.ref.id == artist_id
        releases = await demo.artist_releases(artist_id)
        assert releases, artist.name
        for release in releases:
            assert (await demo.album(release.ref.id)).tracks
        for song in await demo.top_songs(artist_id):
            assert song.album is not None and (await demo.album(song.album.id)).tracks
    # An artist with a record keeps its own name; one of a duo's is its part of the credit.
    duo = await demo.album(album_id("album-duo-deluxe-explicit"))
    names = [(await demo.artist(ref.id)).name for ref in duo.artist_refs]
    assert (len(names) == 2 and " & ".join(names) == duo.artist) or duo.artist in names
    # The tests' replay answers exactly what is recorded: an artist without a record is not found.
    replay = Replay()
    unrecorded = next(a for a in sorted(artists) if f"artists/{a}" not in replay.by_path)
    with pytest.raises(CatalogError):
        await replay.catalog().artist(unrecorded)
    assert await replay.catalog().artist_releases(unrecorded) == ()


async def test_a_record_added_while_an_answer_is_made_does_not_break_it() -> None:
    """A test changes the replay's records from its own thread while the server answers."""
    import threading

    replay = Replay()
    demo = replay.catalog()
    stop = threading.Event()

    album = replay.by_path[f"albums/{album_id('album-duo-deluxe-explicit')}"]["body"]
    credit = list(album["artist_refs"])
    unrecorded = next(r["id"] for r in credit if f"artists/{r['id']}" not in replay.by_path)

    def churn() -> None:
        n = 0
        while not stop.is_set():
            n += 1
            replay.by_path[f"songs/extra{n % 50}"] = {"path": "x", "status": 404}
            replay.by_path.pop(f"songs/extra{(n + 25) % 50}", None)
            album["artist_refs"][:] = credit[::-1] if n % 2 else credit  # a nested edit

    thread = threading.Thread(target=churn)
    thread.start()
    try:
        named = DemoCatalog(names=True)
        named.by_path = replay.by_path  # the demo's own lookups, over the changing records
        for _ in range(200):
            await demo.songs_by_isrc("ZZSHJ0000146")
            await demo.song("900000206")
            assert (await named.artist(unrecorded)).name  # known by the album's credit
    finally:
        stop.set()
        thread.join()
