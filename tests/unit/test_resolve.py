"""The rules a lookup's answer is accepted by - a ``/resolve`` item, a search's tracks
(``mismatches``, ``best_match``) - and the order of the lookups, against a mocked add-on."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from shijhon.delivery import pacing
from shijhon.delivery.addon import (
    Addon,
    AddonError,
    Found,
    Wanted,
    album_version,
    best_match,
    mismatches,
)
from shijhon.delivery.download_first import track_of

WANTED = Wanted("ZZSHJ9900001", "Glass Orchard", "Velane", 200_000)
CLEAN = Wanted("ZZSHJ9900001", "Glass Orchard", "Velane", 200_000, "clean")  # by its flag
TITLED_CLEAN = Wanted("ZZSHJ9900001", "Glass Orchard (Clean)", "Velane", 200_000)


async def _resolve(
    item: dict[str, Any] | None, wanted: Wanted = WANTED
) -> tuple[str | None, list[str]]:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json={"id": "t", "name": "T", "resources": ["resolve"]})
        return httpx.Response(200, content=json.dumps({"item": item}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        addon = Addon("https://addon.example.invalid", None, http)
        notes: list[str] = []
        return await addon.resolve_recording(wanted, notes), notes


def _item(**fields: Any) -> dict[str, Any]:
    return {"id": "x1", "title": "Glass Orchard", "artist": "Velane", "durationMs": 201_000,
            **fields}  # fmt: skip


@pytest.mark.anyio
@pytest.mark.parametrize(
    "item,wanted,accepted",
    [
        (_item(), WANTED, True),
        (_item(title="Glass Orchard (Remastered 2011)"), WANTED, True),
        (_item(title="Glass Orchard (Clean)"), WANTED, False),
        (_item(title="Glass Orchard [Clean Version]"), WANTED, False),
        (_item(title="Glass Orchard (Clean)"), CLEAN, True),
        (_item(title="Glass Orchard (Explicit)"), WANTED, True),
        (_item(title="Glass Orchard (Explicit)"), CLEAN, False),
        (_item(), TITLED_CLEAN, False),
        (_item(title="Glass Orchard (Clean)"), TITLED_CLEAN, True),
        # A clean track by the catalog's flag is as strict as one by its title.
        (_item(), CLEAN, False),
        (_item(title="Glass Orchard (feat. Oren) (Clean)"), WANTED, False),
        (_item(title="Glass Orchard (with Clean Lanterns)"), WANTED, True),  # a credit
        # The wanted ISRC identifies the recording whatever the titles say.
        (_item(isrc="ZZSHJ9900001"), CLEAN, True),
        (_item(title="Glass Orchard (Clean)", isrc="ZZ-SHJ-99-00001"), WANTED, True),
    ],
)
async def test_a_clean_edit_is_other_audio(
    item: dict[str, Any], wanted: Wanted, accepted: bool
) -> None:
    found, notes = await _resolve(item, wanted)
    assert (found == "x1") is accepted
    assert bool(notes) is not accepted
    if not accepted:
        assert notes == ["a match was rejected: its title differs"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "title,accepted",
    [
        ("Glass Orchard - 2011 Remaster", True),
        ("Glass Orchard (Remastered Version)", True),
        ("Glass Orchard (Deluxe Edition)", True),  # an edition marker
        ("Glass Orchard (Acoustic Version)", False),
        ("Glass Orchard (Live)", False),
        ("Glass Orchard (Spanish Version)", False),
        ("Glass Orchard (Single Version)", False),
        ("Glass Orchard - Radio Edit", False),
    ],
)
async def test_a_version_marker_on_one_side_only_is_other_audio(title: str, accepted: bool) -> None:
    """Only reissue markers are the same audio - also with the wanted ISRC."""
    for isrc in (None, "ZZSHJ9900001"):
        found, _ = await _resolve(_item(title=title, isrc=isrc))
        assert (found == "x1") is accepted, (title, isrc)


@pytest.mark.anyio
async def test_an_item_without_a_length_needs_the_wanted_isrc() -> None:
    no_length = {"id": "x1", "title": "Glass Orchard", "artist": "Velane"}
    assert (await _resolve({**no_length, "isrc": "ZZ-SHJ-99-00001"}))[0] == "x1"
    found, notes = await _resolve({**no_length, "isrc": "ZZSHJ9900002"})
    assert found is None
    assert notes == ["a match was rejected: its ISRC (no length sent) differs"]
    assert (await _resolve(no_length))[0] is None  # no ISRC either
    without_isrc = Wanted(None, "Glass Orchard", "Velane", 200_000)
    assert (await _resolve({**no_length, "isrc": "ZZSHJ9900001"}, without_isrc))[0] is None
    # With a length, the ISRC is not needed (remasters carry new ones).
    assert (await _resolve(_item(isrc="ZZSHJ9900009")))[0] == "x1"


@pytest.mark.parametrize(
    "tags,version",
    [
        ({"itunesadvisory": ["2"]}, "clean"),
        ({"itunesadvisory": ["1"]}, "explicit"),
        ({}, None),
        ("not json", None),
        ({"itunesadvisory": []}, None),
    ],
)
def test_a_placeholder_s_advisory_tag_is_its_version(tags: Any, version: str | None) -> None:
    row = {"song_id": "s1", "isrc": None, "title": "T", "artist": "A", "duration_ms": 1000,
           "track_ref": "x:1", "release_ref": "x:r", "disc": 1, "track": 1,
           "tags": tags if isinstance(tags, str) else json.dumps(tags)}  # fmt: skip
    assert track_of(row).wanted.version == version


def test_a_list_or_a_table_from_the_configuration_file_is_not_sent() -> None:
    """Add-on settings are query parameters: text, a number, on or off. A list or a table
    (possible in the configuration file) went out as an empty value, over the add-on's own
    default; it is left out."""
    settings = {
        "quality": 6,
        "hq": True,
        "region": "",
        "mirrors": ["a.example.invalid", "b.example.invalid"],
        "extra": {"depth": 2},
    }
    addon = Addon("https://addon.example.invalid/x?key=1", settings, None)  # type: ignore[arg-type]
    asked = dict(addon._url("resolve-isrc", {"isrc": "ZZ0000000001"}).params)
    assert asked == {"key": "1", "quality": "6", "hq": "true", "region": "", "isrc": "ZZ0000000001"}


# --- the shared matcher ----------------------------------------------------------------


def _found(**fields: Any) -> Found:
    base: dict[str, Any] = {"id": "x1", "title": "Glass Orchard", "artist": "Velane",
                            "duration_ms": 201_000, "isrc": None}  # fmt: skip
    return Found(**(base | fields))


@pytest.mark.parametrize(
    "found,wanted,differs",
    [
        (_found(), WANTED, []),
        (_found(duration_ms=203_000), WANTED, []),  # within 3 s
        (_found(duration_ms=204_001), WANTED, ["length"]),
        (_found(artist="Morrow Lane"), WANTED, ["artist"]),
        (_found(artist="Velane & Oren"), WANTED, []),  # one artist in common
        (_found(title="Glass Orchard (Live)"), WANTED, ["title"]),
        (_found(title="Glass Orchard (Live)", isrc="ZZSHJ9900001"), WANTED, ["title"]),
        (_found(title="Glass Orchard - 2011 Remaster"), WANTED, []),
        (_found(title="Glass Orchard (Clean)"), WANTED, ["title"]),
        (_found(title="Glass Orchard (Clean)", isrc="ZZSHJ9900001"), WANTED, []),
        (_found(), CLEAN, ["title"]),
        (_found(title="Glass Orchard (Clean)"), CLEAN, []),
        (_found(duration_ms=None), WANTED, ["ISRC (no length sent)"]),
        (_found(duration_ms=0), WANTED, ["ISRC (no length sent)"]),
        (_found(duration_ms=None, isrc="ZZSHJ9900001"), WANTED, []),
        (
            _found(duration_ms=None, isrc="-"),
            Wanted("-", "Glass Orchard", "Velane", 200_000),
            ["ISRC (no length sent)"],
        ),  # a junk ISRC is none
        (_found(title=None, artist=None, duration_ms=1), WANTED, ["title", "artist", "length"]),
    ],
)
def test_mismatches_name_what_tells_a_track_from_the_wanted_recording(
    found: Found, wanted: Wanted, differs: list[str]
) -> None:
    assert mismatches(found, wanted) == differs


def test_best_match_takes_the_best_acceptable_track_and_none_doubtful() -> None:
    live = _found(id="live", title="Glass Orchard (Live)", duration_ms=200_000)
    far = _found(id="far", duration_ms=202_500)
    near = _found(id="near", duration_ms=200_400)
    other = _found(id="other", artist="Morrow Lane", duration_ms=200_000)
    assert best_match([live, far, near, other], WANTED)[0] == near
    assert best_match([far, near], WANTED)[0] == near
    assert best_match([near, _found(id="twin", duration_ms=200_400)], WANTED)[0] == near
    # The wanted ISRC comes first, then a length.
    carries = _found(id="isrc", duration_ms=202_900, isrc="ZZ-SHJ-99-00001")
    assert best_match([near, carries], WANTED)[0] == carries
    # A track that carries another ISRC (perhaps another recording) after one with none.
    elsewhere = _found(id="elsewhere", duration_ms=200_000, isrc="ZZSHJ9900077")
    assert best_match([elsewhere, far], WANTED)[0] == far
    assert best_match([elsewhere, far], Wanted(None, "Glass Orchard", "Velane", 200_000))[0] == (
        elsewhere)  # fmt: skip
    bare = _found(id="bare", duration_ms=None, isrc="ZZSHJ9900001")
    assert best_match([bare, carries], WANTED)[0] == carries
    assert best_match([bare], WANTED)[0] == bare
    # The version the wanted track's title or flag names.
    marked = _found(id="marked", title="Glass Orchard (Explicit)", duration_ms=202_000)
    explicit = Wanted("ZZSHJ9900001", "Glass Orchard", "Velane", 200_000, "explicit")
    assert best_match([near, marked], explicit)[0] == marked
    assert best_match([near, marked], WANTED)[0] == marked  # explicit by default
    # None of them: nothing, and why the closest was not it.
    assert best_match([live, other, _found(id="bad", artist="Oren", title="Other")], WANTED) == (
        None, ["title"])  # fmt: skip
    assert best_match([], WANTED) == (None, [])


# --- the search at an add-on without /resolve ------------------------------------------


@dataclass
class Body:
    """A search's answer as it is sent (not a list of tracks)."""

    body: Any


class Searchable:
    """An add-on behind a mock transport with the resources ``resources``: its search
    answers ``answer`` (a status, a list of tracks, or the answer as it is)."""

    def __init__(self, resources: list[str], answer: Any) -> None:
        self.resources = resources
        self.answer = answer
        self.asked: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        path = request.url.path
        if path.endswith("manifest.json"):
            return httpx.Response(200, json={"id": "t", "name": "T", "resources": self.resources})
        if path.endswith("/search"):
            if isinstance(self.answer, int):
                return httpx.Response(self.answer, json={"error": "x"})
            if isinstance(self.answer, list):
                return httpx.Response(200, json={"tracks": self.answer, "albums": []})
            return httpx.Response(200, content=json.dumps(self.answer.body))
        if path.endswith("/resolve-isrc"):
            return httpx.Response(404, json={"error": "not found"})
        if path.endswith("/resolve"):
            return httpx.Response(200, json={"item": None})
        return httpx.Response(404)

    def endpoints(self) -> list[str]:
        return [request.url.path.rsplit("/", 1)[-1] for request in self.asked]


async def _find(
    server: Searchable, wanted: Wanted = WANTED, pace: pacing.AddonPace | None = None
) -> tuple[str | None, list[str]]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as http:
        addon = Addon("https://addon.example.invalid/cfg", None, http, pace)
        notes: list[str] = []
        return await addon.find(wanted, notes), notes


def _track(**fields: Any) -> dict[str, Any]:
    return {"id": "s1", "title": "Glass Orchard", "artist": "Velane", "duration": 201, **fields}


@pytest.mark.anyio
async def test_an_add_on_without_resolve_is_searched_by_artist_and_title() -> None:
    server = Searchable(["search", "stream"], [_track()])
    assert await _find(server) == ("s1", [])
    assert server.endpoints() == ["manifest.json", "search"]
    assert server.asked[1].url.params["q"] == "Velane Glass Orchard"
    # After the ISRC lookup, when it has one and misses.
    server = Searchable(["isrc", "search", "stream"], [_track()])
    assert (await _find(server))[0] == "s1"
    assert server.endpoints() == ["manifest.json", "resolve-isrc", "search"]


@pytest.mark.anyio
async def test_an_add_on_with_resolve_is_never_searched() -> None:
    for resources in (["resolve", "search", "stream"], ["isrc", "resolve", "search", "catalog"]):
        server = Searchable(resources, [_track()])
        assert (await _find(server))[0] is None
        assert "search" not in server.endpoints()
        assert "resolve" in server.endpoints()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "track,note",
    [
        (_track(duration=207), "its length differs"),
        (_track(title="Glass Orchard (Live)"), "its title differs"),
        (_track(title="Glass Orchard - Extended Mix"), "its title differs"),
        (_track(title="Glass Orchard (Clean)"), "its title differs"),
        (_track(artist="Morrow Lane"), "its artist differs"),
        (_track(duration=None), "its ISRC (no length sent) differs"),
        (_track(artist="Morrow Lane", duration=300), "its artist and length differ"),
    ],
)
async def test_a_search_result_that_is_another_recording_is_refused(
    track: dict[str, Any], note: str
) -> None:
    found, notes = await _find(Searchable(["search", "stream"], [track]))
    assert found is None
    assert notes == [f"a match was rejected: {note} (the search's one track)"]


@pytest.mark.anyio
async def test_a_search_s_first_100_tracks_are_compared() -> None:
    later = [_track(id=f"live{n}", title="Glass Orchard (Live)") for n in range(100)]
    found, notes = await _find(Searchable(["search", "stream"], [*later, _track(id="s101")]))
    assert found is None
    assert notes == ["a match was rejected: its title differs (the closest of 100 tracks searched)"]
    # A title or an artist of more than 500 characters is no track's.
    for title, artist in (("G" * 501, "Velane"), ("Glass Orchard", "V" * 501)):
        wanted = Wanted(None, title, artist, 200_000)
        long = [_track(id="t", title=title, artist=artist)]
        assert (await _find(Searchable(["search", "stream"], long), wanted))[0] is None
    wanted = Wanted(None, "G" * 500, "V" * 500, 200_000)
    bounded = [_track(id="t", title="G" * 500, artist="V" * 500)]
    assert (await _find(Searchable(["search", "stream"], bounded), wanted))[0] == "t"


@pytest.mark.anyio
async def test_a_search_is_read_leniently() -> None:
    tracks = [
        "junk",
        {"title": "Glass Orchard", "artist": "Velane", "duration": 200},  # no ID: no use
        {"id": 7, "name": "Glass Orchard", "artist": {"name": "Velane"}, "durationMs": 200_500},
    ]
    assert (await _find(Searchable(["search", "stream"], tracks)))[0] == "7"
    listed = [_track(id="s2", artist=[{"name": "Oren"}, "Velane"])]
    assert (await _find(Searchable(["search", "stream"], listed)))[0] == "s2"
    titled = [_track(id="s3", artist={"title": "Velane"})]
    assert (await _find(Searchable(["search", "stream"], titled)))[0] == "s3"
    for body in ({"tracks": "none"}, ["not", "an", "object"], {"albums": []}, None):
        assert await _find(Searchable(["search", "stream"], Body(body))) == (None, [])


@pytest.mark.anyio
async def test_several_results_the_best_one_and_a_note_for_none() -> None:
    tracks = [
        _track(id="live", title="Glass Orchard (Live)"),
        _track(id="far", duration=202.8),
        _track(id="other", artist="Morrow Lane"),
        _track(id="near", durationMs=200_100),
    ]
    assert (await _find(Searchable(["search", "stream"], tracks)))[0] == "near"
    # The wanted ISRC settles it: a track marked "(Clean)" that carries it is the wanted one.
    settled = [_track(id="near"), _track(id="isrc", title="Glass Orchard (Clean)",
                                         isrc="ZZSHJ9900001", duration=202)]  # fmt: skip
    assert (await _find(Searchable(["search", "stream"], settled)))[0] == "isrc"
    found, notes = await _find(Searchable(["search", "stream"], tracks[:1] + tracks[2:3]))
    assert found is None
    assert notes == ["a match was rejected: its title differs (the closest of 2 tracks searched)"]


@pytest.mark.anyio
@pytest.mark.parametrize("status,kind", [(429, "rate_limited"), (503, "failed"), (410, "expired")])
async def test_a_search_error_is_raised_as_resolve_s_is(status: int, kind: str) -> None:
    with pytest.raises(AddonError) as raised:
        await _find(Searchable(["search", "stream"], status))
    assert raised.value.kind == kind


@pytest.mark.anyio
@pytest.mark.parametrize("status", [404, 403])
async def test_a_search_that_is_not_here_finds_nothing(status: int) -> None:
    assert await _find(Searchable(["search", "stream"], status)) == (None, [])


@pytest.mark.anyio
async def test_a_search_takes_its_turn_at_the_add_on_s_limit_and_its_429_counts() -> None:
    pace = pacing.AddonPace(pacing.Limits(0, 1, 0))
    assert (await _find(Searchable(["search", "stream"], [_track()]), pace=pace))[0] == "s1"
    assert pace.sent == 2  # the manifest and the search
    with pytest.raises(AddonError):
        await _find(Searchable(["search", "stream"], 429), pace=pace)
    assert pace.blocked > 0  # left alone
    with pytest.raises(AddonError) as raised:
        await _find(Searchable(["search", "stream"], [_track()]), pace=pace)
    assert raised.value.kind == "cooling"


# --- clean and explicit twins of a search, neither with the wanted ISRC -------------------

UNKNOWN = Wanted(None, "Glass Orchard", "Velane", 200_000)  # no version said: explicit first
EXPLICIT = Wanted(None, "Glass Orchard", "Velane", 200_000, "explicit")
WANTED_CLEAN = Wanted(None, "Glass Orchard (Clean)", "Velane", 200_000)  # by its title
FLAGGED_CLEAN = Wanted(None, "Glass Orchard", "Velane", 200_000, "clean")  # by the flag


def _searched(**fields: Any) -> Found:
    return Found.searched(_track(**fields))


@pytest.mark.parametrize(
    "fields,explicit",
    [
        ({"explicit": True}, True),
        ({"explicit": False}, False),
        ({"isExplicit": True}, True),
        ({"explicit": "true"}, True),
        ({"explicit": " Explicit "}, True),
        ({"explicit": "false"}, False),
        ({"explicit": "clean"}, False),
        ({"explicit": "Not Explicit"}, False),
        ({"explicit": 1}, True),
        ({"explicit": 0}, False),
        ({"explicit": None, "isExplicit": False}, False),
        ({"explicit": "maybe"}, None),
        ({"explicit": 2}, None),
        ({}, None),
    ],
)
def test_a_search_track_s_explicit_flag_is_read_leniently(
    fields: dict[str, Any], explicit: bool | None
) -> None:
    assert _searched(**fields).explicit is explicit


@pytest.mark.parametrize(
    "album,version",
    [
        ("Night Weather", None),
        ("Night Weather (Clean)", "clean"),
        ("Night Weather (Clean Version)", "clean"),
        ("Night Weather [Edited]", "clean"),
        ("Night Weather (Edited Version)", "clean"),
        ("Night Weather - Edited", "clean"),
        ("Night Weather (Explicit)", "explicit"),
        ("Edited Lines", None),  # a word of the title, no marker
        ({"title": "Night Weather (Edited)"}, "clean"),
    ],
)
def test_a_search_track_s_album_says_its_version(album: Any, version: str | None) -> None:
    assert album_version(_searched(album=album).album) == version


def _twins(*variants: dict[str, Any]) -> list[Found]:
    """Acceptable tracks for the wanted song, alike but for ``variants``; the first is the
    closest in length (so that length alone would choose it)."""
    return [
        _searched(id=f"v{n}", durationMs=200_000 + 1000 * n, **variant)
        for n, variant in enumerate(variants)
    ]


@pytest.mark.parametrize("wanted", [UNKNOWN, EXPLICIT])
def test_twins_the_explicit_one_by_its_flag_then_its_album(wanted: Wanted) -> None:
    # 1. The flag: explicit first, then none, a track flagged not explicit last.
    assert best_match(_twins({"explicit": False}, {"explicit": True}), wanted)[0].id == "v1"  # type: ignore[union-attr]
    assert best_match(_twins({"explicit": False}, {}), wanted)[0].id == "v1"  # type: ignore[union-attr]
    assert best_match(_twins({}, {"isExplicit": "explicit"}), wanted)[0].id == "v1"  # type: ignore[union-attr]
    # ... before the album: a flagged explicit track on an "(Edited)" album still wins.
    flagged = _twins(
        {"album": "Night Weather"}, {"explicit": True, "album": "Night Weather (Edited)"}
    )
    assert best_match(flagged, wanted)[0].id == "v1"  # type: ignore[union-attr]
    # 2. The album's title: an edited or clean album's track after an unmarked one.
    for marked in ("Night Weather (Clean)", "Night Weather (Edited Version)"):
        twins = _twins({"album": marked}, {"album": "Night Weather"})
        assert best_match(twins, wanted)[0].id == "v1"  # type: ignore[union-attr]
    twins = _twins({"album": "Night Weather"}, {"album": "Night Weather (Explicit)"})
    assert best_match(twins, wanted)[0].id == "v1"  # type: ignore[union-attr]
    # 3. Nothing said: the closer length.
    assert best_match(_twins({}, {}), wanted)[0].id == "v0"  # type: ignore[union-attr]
    plain = _twins({"album": "Night Weather"}, {"album": "Night Weather"})
    assert best_match(plain, wanted)[0].id == "v0"  # type: ignore[union-attr]


@pytest.mark.parametrize("wanted", [WANTED_CLEAN, FLAGGED_CLEAN])
def test_twins_for_a_clean_song_the_clean_one(wanted: Wanted) -> None:
    """The same steps, mirrored. (A clean song takes only tracks titled "(Clean)".)"""
    clean = {"title": "Glass Orchard (Clean)"}
    assert (
        best_match(_twins(clean | {"explicit": True}, clean | {"explicit": False}), wanted)[0].id
        == "v1"
    )  # type: ignore[union-attr]
    assert best_match(_twins(clean | {"explicit": True}, clean), wanted)[0].id == "v1"  # type: ignore[union-attr]
    flagged = _twins(clean | {"album": "Night Weather (Edited)"}, clean | {"explicit": False})
    assert best_match(flagged, wanted)[0].id == "v1"  # type: ignore[union-attr]
    for marked in ("Night Weather (Clean)", "Night Weather [Edited]"):
        twins = _twins(clean | {"album": "Night Weather"}, clean | {"album": marked})
        assert best_match(twins, wanted)[0].id == "v1"  # type: ignore[union-attr]
    twins = _twins(
        clean | {"album": "Night Weather (Explicit)"}, clean | {"album": "Night Weather"}
    )
    assert best_match(twins, wanted)[0].id == "v1"  # type: ignore[union-attr]
    assert best_match(_twins(clean, clean), wanted)[0].id == "v0"  # type: ignore[union-attr]


def test_the_wanted_isrc_decides_before_the_version_signals() -> None:
    """The track with the wanted ISRC wins although every other signal says the other
    version, and its length is the farther."""
    explicit = Wanted("ZZSHJ9900001", "Glass Orchard", "Velane", 200_000)
    twins = _twins(
        {"explicit": True, "album": "Night Weather"},
        {"isrc": "ZZSHJ9900001", "explicit": False, "album": "Night Weather (Edited)"},
    )
    assert best_match(twins, explicit)[0].id == "v1"  # type: ignore[union-attr]
    clean = Wanted("ZZSHJ9900001", "Glass Orchard (Clean)", "Velane", 200_000)
    twins = _twins(
        {"title": "Glass Orchard (Clean)", "explicit": False, "album": "Night Weather (Edited)"},
        {"isrc": "ZZSHJ9900001", "explicit": True, "album": "Night Weather (Explicit)"},
    )
    assert best_match(twins, clean)[0].id == "v1"  # type: ignore[union-attr]
