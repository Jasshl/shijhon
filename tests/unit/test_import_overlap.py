"""The review's decisions for music the library already has (invented music)."""

from __future__ import annotations

from typing import Any

import anyio

from shijhon.music_imports.overlap import (
    CATALOG_SONG,
    COPY,
    DUPLICATE,
    HELD,
    LINKED,
    NEW,
    UNSURE,
    Incoming,
    LibraryAlbum,
    LibrarySong,
    classify,
)
from shijhon.quality import Quality

ARTIST, ALBUM = "Tide Paper", "Night Letters"


def incoming(
    ident: str,
    title: str,
    number: int | None,
    *,
    album: str = ALBUM,
    seconds: float = 200.0,
    fmt: str = "flac",
    kbps: int = 900,
    release: str = "",
    date: str = "2020",
    digest: str = "",
    distinct: bool = False,
    taggable: bool = True,
    isrcs: tuple[str, ...] = (),
    disc: int = 1,
) -> Incoming:
    tags: dict[str, list[str]] = {"album": [album] if album else [], "albumartist": [ARTIST]}
    if release:
        tags["releasedate"] = [release]
    if date:
        tags["recordingdate"] = [date]
    facts: dict[str, Any] = {
        "title": title,
        "artist": ARTIST,
        "album": album,
        "albumartist": ARTIST if album else "",
        "track": number,
        "disc": disc,
        "isrcs": list(isrcs),
        "recording_id": "",
        "clean": None,
        "album_tags": {k: v for k, v in tags.items() if v},
        "taggable": taggable,
        "duration_ms": int(seconds * 1000),
        "quality": {"format": fmt, "lossless": fmt == "flac", "kbps": kbps, "sample_rate": 44100},
    }
    return Incoming(ident, facts, digest=digest, distinct=distinct)


def library(
    song_id: str,
    title: str,
    number: int | None,
    *,
    album_id: str = "al-1",
    album: str = ALBUM,
    seconds: float = 200.0,
    year: int = 2020,
    placeholder: bool = False,
    isrcs: tuple[str, ...] = (),
) -> LibrarySong:
    return LibrarySong(
        song_id=song_id,
        track_key="native:" + song_id,
        album_id=album_id,
        title=title,
        artist=ARTIST,
        album=album,
        album_artist=ARTIST,
        year=year,
        disc=1,
        number=number,
        duration_ms=int(seconds * 1000),
        isrcs=isrcs,
        quality=Quality("mp3", kbps=320),
        placeholder=placeholder,
    )


NATIVE_TAGS = {"album": [ALBUM], "albumartist": [ARTIST], "recordingdate": ["2020"]}
EXISTING = [library(f"s{n}", f"Lantern {n}", n) for n in range(1, 5)]


def decide(items: list[Incoming], songs: list[LibrarySong], **kw: Any) -> dict[str, Any]:
    async def tags(album: LibraryAlbum) -> Any:
        return kw.pop("tags", NATIVE_TAGS)

    async def run() -> dict[str, Any]:
        return await classify(items, songs, album_tags=tags, **kw)

    return {k: v for k, v in anyio.run(run).items()}


def outcomes(result: dict[str, Any]) -> dict[str, str]:
    return {k: v.outcome for k, v in result.items()}


def test_same_album_other_format_and_other_tags_link_to_the_library() -> None:
    mp3 = [incoming(f"m{n}", f"Lantern {n}", n, fmt="mp3", kbps=128) for n in range(1, 5)]
    result = decide(mp3, EXISTING)
    assert outcomes(result) == {f"m{n}": LINKED for n in range(1, 5)}
    assert result["m2"].song.song_id == "s2"
    # A release date only in these files: still the same album (they share songs).
    dated = [incoming(f"d{n}", f"Lantern {n}", n, release="2020-03-01") for n in range(1, 5)]
    assert set(outcomes(decide(dated, EXISTING)).values()) == {LINKED}


def test_exact_copy_links_even_without_an_album() -> None:
    exact = library("s9", "Untitled", None, album="", album_id="al-9")
    item = incoming("u", "Untitled", None, album="", digest="abc")
    result = decide([item], [], imported={"abc": exact})
    assert result["u"].outcome == LINKED and result["u"].song.song_id == "s9"
    assert outcomes(decide([incoming("v", "Untitled", None, album="")], [])) == {"v": NEW}


def test_partial_overlap_adds_the_missing_songs_into_the_library_album() -> None:
    items = [incoming("a", "Lantern 1", 1), incoming("b", "Lantern 5", 5, seconds=210)]
    result = decide(items, EXISTING)
    assert outcomes(result) == {"a": LINKED, "b": NEW}
    assert result["b"].album.album_id == "al-1"
    # Tags that would make a second album: a copy with the library album's tags.
    dated = [incoming("c", "Lantern 5", 5, seconds=210, release="2020-03-01")]
    assert outcomes(decide([*dated, incoming("x", "Lantern 1", 1)], EXISTING)) == {
        "c": COPY,
        "x": LINKED,
    }
    # A format whose tags cannot be copied is held back.
    held = [incoming("h", "Lantern 5", 5, seconds=210, release="2020", taggable=False)]
    assert outcomes(decide([*held, incoming("y", "Lantern 2", 2)], EXISTING))["h"] == HELD


def test_other_editions_singles_and_versions_are_their_own() -> None:
    deluxe = [
        incoming(f"x{n}", f"Lantern {n}", n, album="Night Letters (Deluxe Edition)")
        for n in range(1, 7)
    ]
    assert set(outcomes(decide(deluxe, EXISTING)).values()) == {NEW}
    single = [incoming("s", "Lantern 1", 1, album="Lantern 1 - Single")]
    assert outcomes(decide(single, EXISTING)) == {"s": NEW}
    versions = [
        incoming("live", "Lantern 1 (Live)", 9),
        incoming("remix", "Lantern 2 (Remix)", 10),
        incoming("clean", "Lantern 3 (Clean)", 11),
        incoming("same", "Lantern 4", 4),
    ]
    result = outcomes(decide(versions, EXISTING))
    assert result == {"live": NEW, "remix": NEW, "clean": NEW, "same": LINKED}


def test_same_name_other_album_or_second_disc() -> None:
    # A self-titled pair: another year and no song in common - another album.
    other = [incoming("o1", "Ember", 1, date="2023")]
    assert outcomes(decide(other, EXISTING)) == {"o1": NEW}
    assert decide(other, EXISTING)["o1"].album is None
    # The same year and no song in common (a second disc): the covers decide.
    disc_two = [incoming("d1", "Ember", 1, disc=2)]

    async def same(items: Any, album: LibraryAlbum) -> bool | None:
        return False

    async def different(items: Any, album: LibraryAlbum) -> bool | None:
        return True

    assert decide(disc_two, EXISTING, covers_differ=same)["d1"].album.album_id == "al-1"
    assert decide(disc_two, EXISTING, covers_differ=different)["d1"].album is None


def test_several_same_named_albums_use_the_one_with_most_songs() -> None:
    small = [library("t1", "Lantern 1", 1, album_id="al-2")]
    result = decide([incoming("a", "Lantern 1", 1)], EXISTING + small)
    assert result["a"].song.album_id == "al-1"


def test_catalog_songs_unsure_matches_and_duplicates_in_the_selection() -> None:
    catalog = [library("p5", "Lantern 5", 5, placeholder=True, seconds=210)]
    result = decide([incoming("c", "Lantern 5", 5, seconds=210)], EXISTING + catalog)
    assert result["c"].outcome == CATALOG_SONG and result["c"].song.song_id == "p5"
    # Two different recordings of one title in the library, and nothing tells which.
    twins = [
        library("i1", "Interlude", None, isrcs=("XXA0L2000101",)),
        library("i2", "Interlude", None, seconds=201, isrcs=("XXA0L2000102",)),
    ]
    assert outcomes(decide([incoming("u", "Interlude", None, seconds=200.5)], twins)) == {
        "u": UNSURE
    }
    # FLAC and MP3 of one album in one selection: the better copy goes on.
    both = [
        incoming("f", "Ember", 1, album="Paper Garden"),
        incoming("m", "Ember", 1, album="Paper Garden", fmt="mp3", kbps=320),
    ]
    result = decide(both, [])
    assert outcomes(result) == {"f": NEW, "m": DUPLICATE} and result["m"].of == "f"


def test_two_copies_of_one_album_in_the_selection_become_one_album() -> None:
    first = [incoming(f"a{n}", f"Ember {n}", n, album="Paper Garden") for n in (1, 2, 3)]
    second = [
        incoming("b2", "Ember 2", 2, album="Paper Garden", release="2021-05-01"),
        incoming("b4", "Ember 4", 4, album="Paper Garden", release="2021-05-01", seconds=190),
    ]
    result = decide(first + second, [])
    assert result["b2"].outcome == DUPLICATE
    assert result["b4"].outcome == COPY and result["b4"].tags_from.startswith("item:a")


def test_not_the_same_song_publishes_as_it_is() -> None:
    items = [incoming("a", "Lantern 1", 1, distinct=True), incoming("b", "Lantern 2", 2)]
    assert outcomes(decide(items, EXISTING)) == {"a": NEW, "b": LINKED}


def test_album_identity_agrees_with_the_fill_prediction() -> None:
    from shijhon.fill.fills import _identity
    from shijhon.matching.grouping import album_key, identity

    for tag in (
        "Tide Paper",
        "It\u2019s \u201cNight\u201d \u2014 Letters",
        "\u03a3\u0391\u03a3",
        " Ember ",
    ):
        assert identity(tag) == _identity(tag)
    tags = {"album": ["Night Letters"], "albumartist": ["TIDE PAPER"], "releasedate": ["2020"]}
    assert album_key(tags) == album_key({**tags, "albumartist": ["Tide Paper"]})
    assert album_key(tags) != album_key({**tags, "releasedate": ["2020-03-01"]})
    assert album_key(tags) != album_key({**tags, "albumversion": ["Deluxe"]})
    assert album_key({**tags, "musicbrainz_albumid": ["X"]}) == album_key(
        {"musicbrainz_albumid": ["x"]}
    )
    assert album_key({"album": ["Night Letters"]}, "Tide Paper") == album_key(
        {"album": ["night letters"], "albumartist": ["Tide Paper"]}
    )


def native_row(song_id: str, title: str, number: int, **fields: Any) -> tuple[dict, dict]:
    """A ``collection_tracks`` row and its stored native song fields."""
    row = {"song_id": song_id, "track_key": "native:" + song_id, "album_id": "al-1"}
    metadata = {
        "title": title,
        "artist": ARTIST,
        "album": ALBUM,
        "albumArtist": ARTIST,
        "year": 2020,
        "trackNumber": number,
        "discNumber": 1,
        "duration": 200.0,
        "suffix": "flac",
        "bitRate": 900,
        **fields,
    }
    return row, metadata


def test_library_songs_read_navidromes_native_recording_fields() -> None:
    row, metadata = native_row(
        "n1",
        "Lantern 1",
        1,
        tags={"isrc": ["XXA0L2000001"]},
        mbzRecordingID="rec-1",
        explicitStatus="c",
        hasCoverArt=True,
        mbzAlbumId="alb-1",
        sampleRate=48000,
        bitDepth=24,
    )
    song = LibrarySong.of_index(row, metadata, placeholder=False)
    assert (song.number, song.disc, song.isrcs, song.recording_id) == (
        1,
        1,
        ("XXA0L2000001",),
        "rec-1",
    )
    assert song.clean is True and song.cover == "mf-n1" and song.album_mbid == "alb-1"
    assert song.quality.sample_rate == 48000 and song.quality.bit_depth == 24
    # Conflicting ISRCs are two recordings; the same one is linked.
    other = incoming("x", "Lantern 1", 1, isrcs=("XXA0L2000009",))
    same = incoming("y", "Lantern 1", 1, fmt="mp3", kbps=128, isrcs=("XXA0L2000001",))
    assert decide([other], [song])["x"].outcome == NEW
    assert decide([same], [song])["y"].outcome == LINKED
    # A clean edit is never the explicit song.
    explicit = LibrarySong.of_index(*native_row("n2", "Lantern 2", 2, explicitStatus="e"),
                                    placeholder=False)  # fmt: skip
    clean = incoming("c", "Lantern 2", 2)
    clean = Incoming("c", {**clean.facts, "clean": True})
    assert decide([clean], [explicit])["c"].outcome != LINKED


def test_a_compilation_without_an_album_artist_is_one_album_of_various_artists() -> None:
    def performer(ident: str, title: str, number: int, artist: str) -> Incoming:
        item = incoming(ident, title, number, album="Harbor Lights")
        tags = {"album": ["Harbor Lights"], "compilation": ["1"], "recordingdate": ["2020"]}
        return Incoming(ident, {**item.facts, "artist": artist, "albumartist": "",
                                "album_tags": tags})  # fmt: skip

    items = [performer("a", "Gull", 1, "Ada Quill"), performer("b", "Tern", 2, "Bo Marsh")]
    assert items[0].album_artist == "Various Artists"
    assert outcomes(decide(items, [])) == {"a": NEW, "b": NEW}
    library = []
    for n, title, artist in ((1, "Gull", "Ada Quill"), (2, "Tern", "Bo Marsh")):
        row, metadata = native_row(f"v{n}", title, n, artist=artist, compilation=True)
        metadata.update(album="Harbor Lights", albumArtist="Various Artists")
        library.append(LibrarySong.of_index(row, metadata, placeholder=False))
    assert outcomes(decide(items, library)) == {"a": LINKED, "b": LINKED}


def test_unsure_and_not_the_same_song_still_join_the_album_of_their_name() -> None:
    twins = [
        library("i1", "Interlude", None, isrcs=("XXA0L2000101",)),
        library("i2", "Interlude", None, seconds=201, isrcs=("XXA0L2000102",)),
        *EXISTING,
    ]
    dated = incoming("u", "Interlude", None, seconds=200.5, release="2020-03-01")
    result = decide([dated], twins)["u"]
    assert result.outcome == UNSURE and result.tags_from == "album:al-1"
    assert result.target_key == ("tags", "tide paper", "night letters", "", "")
    # "Not the same song" on a linked file: published into the album, as a copy here.
    said = incoming("d", "Lantern 1", 1, release="2021-01-01", distinct=True)
    assert decide([said], EXISTING)["d"].outcome == COPY
    # A file that cannot carry the album's tags is held: never an album of its own.
    held = incoming("o", "Lantern 1", 1, release="2021-01-01", distinct=True, taggable=False)
    result = decide([held], EXISTING)["o"]
    assert result.outcome == HELD and "second album" in result.reason


def test_a_file_without_an_album_name_never_joins_an_album() -> None:
    """Shorten has no tags at all (``facts.of_ffprobe``): no album name, so never the
    same album as another - published alone, never held or copied."""
    from shijhon.music_imports.facts import of_ffprobe

    facts = of_ffprobe({}, "shn", 200.0, 44100, "shorten")
    assert facts["album"] == "" and facts["album_tags"] == {} and not facts["taggable"]
    result = decide([Incoming("s", facts)], EXISTING)["s"]
    assert result.outcome == NEW and result.album is None and not result.tags_from
    # Matroska's tags are read the Navidrome way, and its copy can carry an album's.
    tags = {"title": "Lantern 5", "ARTIST": ARTIST, "ALBUM": ALBUM, "ALBUM_ARTIST": ARTIST,
            "DATE_RELEASED": "2020-03-01"}  # fmt: skip
    facts = of_ffprobe(tags, "mka", 210.0, 44100, "flac")
    assert facts["album_tags"]["releasedate"] == ["2020-03-01"] and facts["taggable"]
    assert decide([Incoming("m", facts)], EXISTING)["m"].outcome == COPY


def test_incoming_albums_are_told_apart_by_their_covers_too() -> None:
    first = [incoming(f"a{n}", f"Ember {n}", n, album="Paper Garden") for n in (1, 2)]
    second = [incoming("b5", "Ember 5", 5, album="Paper Garden", release="2020-05-01")]
    pictures: dict[str, Any] = {}

    async def covers(items: Any, other: Any) -> bool | None:
        pictures["asked"] = True
        return pictures["differ"]

    pictures["differ"] = True
    result = decide(first + second, [], covers_differ=covers)
    assert pictures["asked"] and result["b5"].outcome == NEW and not result["b5"].tags_from
    pictures["differ"] = False
    assert decide(first + second, [], covers_differ=covers)["b5"].outcome == COPY


def test_classification_grows_linearly_and_leaves_the_event_loop_free() -> None:
    import time

    count = 3000
    items = [incoming(f"t{n:05d}", f"Track {n}", n, seconds=100 + n % 50) for n in range(count)]
    songs = [library(f"s{n:05d}", f"Track {n}", n, seconds=100 + n % 50) for n in range(count)]
    longest = 0.0

    async def run() -> dict[str, Any]:
        nonlocal longest
        done = False

        async def ticker() -> None:
            nonlocal longest
            last = time.monotonic()
            while not done:
                await anyio.sleep(0.005)
                now = time.monotonic()
                longest = max(longest, now - last)
                last = now

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(ticker)
            started = time.monotonic()
            result = await classify(items, songs, album_tags=lambda a: _tags())
            done = True
        assert time.monotonic() - started < 10
        return result

    async def _tags() -> Any:
        return NATIVE_TAGS

    result = anyio.run(run)
    assert set(outcomes(result).values()) == {LINKED}
    assert longest < 0.5, longest
