"""The import quality gate: an album below the importing person's minimum is added from
the catalog instead, decided per album (invented music)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import anyio
import pytest

from shijhon.music_imports.catalog_route import owned_album
from shijhon.music_imports.overlap import (
    CATALOG_SONG,
    LINKED,
    LOW_QUALITY,
    NEW,
    CatalogFound,
    CatalogUnreachable,
    Decision,
    Incoming,
    LibrarySong,
    classify,
)
from shijhon.quality import ANY, DEFAULT, Minimum
from tests.unit.test_import_overlap import incoming, library

RELEASE = "demo:900000001"


def low(ident: str, title: str, number: int, **kw: Any) -> Incoming:
    return incoming(ident, title, number, fmt="mp3", kbps=128, **kw)


def keep(item: Incoming) -> Incoming:
    return replace(item, facts={**item.facts, "keep_files": True})


class Catalog:
    """Looks up albums: every song it is asked about is on one release, or none."""

    def __init__(self, *, has: bool = True, reachable: bool = True) -> None:
        self.has, self.reachable = has, reachable
        self.asked: list[list[str]] = []

    async def __call__(self, items: Sequence[Incoming]) -> CatalogFound | None:
        self.asked.append(sorted(i.id for i in items))
        if not self.reachable:
            raise CatalogUnreachable
        if not self.has:
            return None
        tracks = {i.id: f"demo:{i.id}" for i in items}
        return CatalogFound(RELEASE, "reviewed", tracks)


def decide(
    files: list[Incoming],
    songs: list[LibrarySong] = (),  # type: ignore[assignment]
    *,
    minimum: Minimum | None = DEFAULT,
    catalog: Catalog | None = None,
) -> dict[str, Decision]:
    async def run() -> dict[str, Decision]:
        return await classify(files, list(songs), minimum=minimum, catalog=catalog)

    return anyio.run(run)


def test_a_low_quality_album_the_catalog_has_comes_from_the_catalog() -> None:
    catalog = Catalog()
    files = [low("a", "Lantern 1", 1), low("b", "Lantern 2", 2)]
    found = decide(files, catalog=catalog)
    assert {d.outcome for d in found.values()} == {LOW_QUALITY}
    evidence = found["a"].evidence(files[0])
    assert evidence["route"] == "catalog" and evidence["release"] == RELEASE
    assert evidence["track"] == "demo:a" and evidence["fingerprint"] == "reviewed"
    assert evidence["below_minimum"] == DEFAULT.describe()
    assert evidence["reason"] == (
        "MP3 128 kbps is below your minimum (compressed at 192 kbps or more; lossless at any"
        " bitrate; 44.1 kHz or more)."
        " The catalog's version is added instead; it plays from the add-ons and counts as"
        " your import."
    )
    assert catalog.asked == [["a", "b"]]


def test_decided_per_album_never_half_files_and_half_catalog_songs() -> None:
    catalog = Catalog()
    files = [low("a", "Lantern 1", 1), incoming("b", "Lantern 2", 2)]  # FLAC beside MP3
    found = decide(files, catalog=catalog)
    assert found["a"].outcome == found["b"].outcome == LOW_QUALITY
    assert (
        found["b"]
        .evidence(files[1])["reason"]
        .startswith("Other files of this album are below your minimum")
    )
    # Another album of the same import is not affected.
    other = incoming("c", "Ember 1", 1, album="Ember Road")
    assert decide([*files, other], catalog=Catalog())["c"].outcome == NEW


def test_what_meets_the_minimum_or_is_unknown_is_not_gated() -> None:
    catalog = Catalog()
    files = [incoming("a", "Lantern 1", 1), incoming("b", "Lantern 2", 2, fmt="mp3", kbps=320)]
    unknown = incoming("c", "Ember 1", 1, album="Ember Road", fmt="aac", kbps=0)
    found = decide([*files, unknown], catalog=catalog)
    assert {d.outcome for d in found.values()} == {NEW}
    assert all("below_minimum" not in d.evidence(files[0]) for d in found.values())
    assert catalog.asked == []


def test_each_persons_minimum() -> None:
    files = [low("a", "Lantern 1", 1)]
    assert decide(files, minimum=ANY, catalog=Catalog())["a"].outcome == NEW
    assert decide(files, minimum=Minimum(kbps=128), catalog=Catalog())["a"].outcome == NEW
    assert decide(files, minimum=None, catalog=Catalog())["a"].outcome == NEW
    lossless = Minimum(kbps=0, lossless_only=True)
    loud = [incoming("b", "Lantern 2", 2, fmt="mp3", kbps=320)]
    assert decide(loud, minimum=lossless, catalog=Catalog())["b"].outcome == LOW_QUALITY


def test_songs_already_in_the_library_stay_linked_whatever_their_quality() -> None:
    songs = [library("s1", "Lantern 1", 1), library("s2", "Lantern 2", 2)]
    catalog = Catalog()
    found = decide([low("a", "Lantern 1", 1), low("b", "Lantern 2", 2)], songs, catalog=catalog)
    assert {d.outcome for d in found.values()} == {LINKED}
    assert catalog.asked == []
    # A new song of that album below the minimum: the album's catalog version for it, the
    # linked one stays linked.
    found = decide([low("a", "Lantern 1", 1), low("c", "Lantern 3", 3)], songs, catalog=catalog)
    assert found["a"].outcome == LINKED and found["c"].outcome == LOW_QUALITY
    assert catalog.asked == [["c"]]


def test_a_catalog_song_is_linked_without_low_quality_audio() -> None:
    songs = [library("p1", "Lantern 1", 1, placeholder=True)]
    files = [low("a", "Lantern 1", 1)]
    found = decide(files, songs, catalog=Catalog())
    assert found["a"].outcome == CATALOG_SONG
    evidence = found["a"].evidence(files[0])
    assert evidence["audio"] is False and evidence["below_minimum"]
    assert "keeps playing from the add-ons" in evidence["reason"]
    # In the minimum: its audio can go into that song (no gate evidence).
    good = [incoming("b", "Lantern 1", 1)]
    assert "audio" not in decide(good, songs, catalog=Catalog())["b"].evidence(good[0])


def test_not_in_the_catalog_the_files_are_added_and_marked() -> None:
    files = [low("a", "Lantern 1", 1), incoming("b", "Lantern 2", 2)]
    for catalog, why in ((Catalog(has=False), "The catalog does not have it"),
                         (None, "No catalog is set up")):  # fmt: skip
        found = decide(files, catalog=catalog)
        assert found["a"].outcome == found["b"].outcome == NEW
        marked = found["a"].evidence(files[0])
        assert marked["route"] == "files" and marked["below_minimum"]
        assert marked["reason"].endswith(f"{why}, so your file is added.")
        assert "below_minimum" not in found["b"].evidence(files[1])  # FLAC: not marked


def test_catalog_unreachable_waits_for_a_choice() -> None:
    files = [low("a", "Lantern 1", 1)]
    found = decide(files, catalog=Catalog(reachable=False))
    assert found["a"].outcome == LOW_QUALITY
    evidence = found["a"].evidence(files[0])
    assert evidence["route"] == "unchecked"
    assert "Check again, or add your files anyway" in evidence["reason"]


def test_add_my_files_anyway_for_the_whole_album() -> None:
    catalog = Catalog()
    files = [keep(low("a", "Lantern 1", 1)), low("b", "Lantern 2", 2)]
    found = decide(files, catalog=catalog)
    assert found["a"].outcome == found["b"].outcome == NEW
    assert catalog.asked == []
    evidence = found["b"].evidence(files[1])
    assert evidence["kept"] is True and evidence["route"] == "files"
    assert evidence["reason"].endswith("Added as you chose.")


def test_not_the_same_song_is_still_gated() -> None:
    songs = [library("s1", "Lantern 1", 1)]
    files = [low("a", "Lantern 1", 1, distinct=True)]
    assert decide(files, songs, catalog=Catalog())["a"].outcome == LOW_QUALITY


def test_a_release_missing_one_of_the_songs_does_not_count() -> None:
    class Partial(Catalog):
        async def __call__(self, items: Sequence[Incoming]) -> CatalogFound | None:
            found = await super().__call__(items)
            assert found is not None
            return CatalogFound(found.release, found.fingerprint, {items[0].id: "demo:x"})

    found = decide([low("a", "Lantern 1", 1), low("b", "Lantern 2", 2)], catalog=Partial())
    assert found["a"].outcome == found["b"].outcome == NEW
    assert found["a"].evidence(incoming("a", "", 1))["route"] == "files"


@pytest.mark.parametrize("totals", [False, True])
def test_owned_album_for_matching_has_no_track_totals(totals: bool) -> None:
    files = [low("a", "Lantern 1", 1), low("b", "Lantern 2", 2, disc=2)]
    if totals:
        files = [replace(f, facts={**f.facts, "track_total": 2}) for f in files]
    album = owned_album(files)
    assert album.title == "Night Letters" and album.artist == "Tide Paper"
    assert [(s.id, s.disc, s.number) for s in album.songs] == [("a", 1, 1), ("b", 2, 2)]
    assert all(s.track_total is None for s in album.songs)  # never "complete by tags"


def test_the_catalog_route_keeps_a_clean_albums_clean_recordings() -> None:
    from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
    from shijhon.matching.matcher import Match, Outcome
    from shijhon.music_imports.catalog_route import CatalogRoute

    clean = [replace(low(f"c{n}", f"Lantern {n}", n), facts={**low(f"c{n}", f"Lantern {n}", n)
             .facts, "clean": True}) for n in (1, 2)]  # fmt: skip
    assert owned_album(clean).clean is True
    assert owned_album([low("u", "Lantern 1", 1)]).clean is None

    def release(flag: bool) -> CatalogRelease:
        tracks = tuple(
            CatalogTrack(
                CatalogRef("demo", f"t{n}"),
                f"Lantern {n}",
                "Tide Paper",
                200_000,
                number=n,
                explicit=not flag,
                clean=flag,
            )
            for n in (1, 2)
        )
        return CatalogRelease(CatalogRef("demo", "r"), "Night Letters", "Tide Paper",
                              ReleaseKind.ALBUM, "2020", tracks)  # fmt: skip

    class Matcher:
        def __init__(self, flag: bool) -> None:
            self.flag = flag
            self.asked: Any = None

        async def match(self, album: Any, *, pick: bool = False) -> Match:
            self.asked = album
            found = release(self.flag)
            links = {t.ref: f"c{t.number}" for t in found.tracks}
            return Match(Outcome.FILL, "", release=found, links=links)

    async def find(flag: bool) -> Any:
        matcher = Matcher(flag)
        found = await CatalogRoute(matcher, None).find(clean)  # type: ignore[arg-type]
        assert matcher.asked.clean is True  # matching prefers the clean edition
        return found

    assert anyio.run(find, True) is not None
    assert anyio.run(find, False) is None  # only the explicit recordings: not this album
