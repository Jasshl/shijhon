"""Imported recording evidence keeps units, identity and reviewed constraints."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest

from shijhon.playlists.import_formats import Entry, parse
from shijhon.playlists.model import PlaylistError, Track
from shijhon.playlists.resolve import choose, native_track, same_recording
from tests.unit.test_playlist_requests import service_at


@pytest.mark.parametrize(
    "header,value,expected",
    [
        ("duration_ms", "8000", 8000),
        ("Duration (ms)", "203500", 203500),
        ("Duration (ms)", "8000.0", 8000),
        ("duration", "203.5", 203500),
        ("duration", ".5", 500),
        ("Duration (seconds)", "203.5", 203500),
        ("Length", "3:23.5", 203500),
        ("Time", "1:03:23.5", 3803500),
        ("duration", "12000", 12000000),
        ("duration_ms", "", 0),
        ("duration", "0", 0),
    ],
)
def test_csv_duration_units_are_explicit(header: str, value: str, expected: int) -> None:
    row = parse(f"artist,title,{header},ISRC\nGlass Orchard,First Light,{value},zz-shj-26-00001\n")[
        0
    ]
    assert row.isrc == "ZZSHJ2600001"
    assert row.duration_ms == expected
    assert row.wanted()["duration_ms"] == expected
    assert row.wanted()["isrc"] == row.isrc


def test_csv_mapping_and_two_duration_columns() -> None:
    row = parse(
        "artist;title;Recording;Seconds;Milliseconds\n"
        "Glass Orchard;First Light;ZZSHJ2600001;3:23.5;203500\n",
        mapping={"isrc": "Recording", "duration": "Seconds", "duration_ms": "Milliseconds"},
    )[0]
    assert row.isrc == "ZZSHJ2600001" and row.duration_ms == 203500
    rows = parse(
        "artist,title,duration,duration_ms\nGlass Orchard,First Light,,203500\n"
        "Glass Orchard,First Light,203.5,\n"
    )
    assert [r.duration_ms for r in rows] == [203500, 203500]


@pytest.mark.parametrize(
    "header,value",
    [
        ("isrc", "not-an-isrc"),
        ("isrc", "ZZSHJ260000"),
        ("duration_ms", "3:23"),
        ("duration_ms", "8000.5"),
        ("duration", "-2"),
        ("duration", "-1"),
        ("duration", "NaN"),
        ("duration", "Infinity"),
        ("duration", "1e20"),
        ("duration", "3:65"),
        ("duration", "1:60:00"),
        ("duration", "3 minutes"),
        ("duration,duration_ms", "203,8000"),
    ],
)
def test_invalid_evidence_rejects_the_entry_without_dropping_it(header: str, value: str) -> None:
    with pytest.raises(PlaylistError, match="Import entry 2"):
        parse(
            f"artist,title,{header}\nGlass Orchard,First Light,{',' if ',' in header else ''}\n"
            f"Tideline,Harbor,{value}\n"
        )


@pytest.mark.parametrize("format", ["xspf", "jspf"])
@pytest.mark.parametrize("prefix", ["isrc:", "urn:isrc:"])
def test_xml_and_json_recording_identifiers_and_millisecond_lengths(
    format: str, prefix: str
) -> None:
    if format == "xspf":
        source = (
            '<playlist xmlns="http://xspf.org/ns/0/"><trackList><track>'
            "<creator>Glass Orchard</creator><title>First Light</title>"
            f"<identifier>{prefix}zz-shj-26-00001</identifier>"
            "<identifier>https://example.invalid/recording</identifier>"
            "<duration>8000</duration></track></trackList></playlist>"
        )
    else:
        source = json.dumps(
            {
                "playlist": {
                    "track": [
                        {
                            "creator": "Glass Orchard",
                            "title": "First Light",
                            "duration": 8000,
                            "identifier": [
                                f"{prefix}zz-shj-26-00001",
                                "https://example.invalid/recording",
                            ],
                        }
                    ]
                }
            }
        )
    row = parse(source, format=format)[0]
    assert row.isrc == "ZZSHJ2600001" and row.duration_ms == 8000


def test_jspf_native_identifier_is_preserved_with_recording_hints() -> None:
    row = parse(
        json.dumps(
            {
                "playlist": {
                    "track": [
                        {
                            "identifier": ["navidrome:native-one", "isrc:ZZSHJ2600001"],
                            "duration": 8000,
                        }
                    ]
                }
            }
        )
    )[0]
    assert row.native_id == "native-one" and row.duration_ms == 8000


@pytest.mark.parametrize("format", ["xspf", "jspf"])
def test_multiple_recording_identifiers_preserve_alternatives(format: str) -> None:
    identifiers = ["isrc:ZZSHJ2600001", "urn:isrc:ZZSHJ2600002", "isrc:zz-shj-26-00001"]
    if format == "xspf":
        source = (
            "<playlist><trackList><track><creator>Glass Orchard</creator>"
            "<title>First Light</title>"
            + "".join(f"<identifier>{code}</identifier>" for code in identifiers)
            + "</track></trackList></playlist>"
        )
    else:
        source = json.dumps(
            {
                "playlist": {
                    "track": [
                        {
                            "creator": "Glass Orchard",
                            "title": "First Light",
                            "identifier": identifiers,
                        }
                    ]
                }
            }
        )
    row = parse(source, format=format)[0]
    assert row.recording_isrcs == ["ZZSHJ2600001", "ZZSHJ2600002"]
    candidate = Track(id="native", artist="Glass Orchard", title="First Light", isrc="ZZSHJ2600002")
    assert choose(row.wanted(), [candidate]) == candidate
    assert choose(row.wanted(), [candidate.model_copy(update={"isrc": "ZZSHJ2600003"})]) is None
    assert Entry.model_validate_json(row.model_dump_json()).recording_isrcs == row.recording_isrcs


@pytest.mark.parametrize("format", ["m3u", "pls"])
def test_native_playlist_lengths_keep_unknown_sentinels_and_entry_boundaries(format: str) -> None:
    if format == "m3u":
        source = (
            '#EXTM3U\n#EXTINF:8.5 group-title="Songs",Glass Orchard - First Light\nAlbum/One.flac\n'
            "#EXTINF:-1,Glass Orchard - Harbor\nAlbum/Two.flac\nAlbum/Three.flac\n"
        )
    else:
        source = (
            "[playlist]\nFile1=Album/One.flac\nTitle1=Glass Orchard - First Light\nLength1=8.5\n"
            "File2=Album/Two.flac\nLength2=-1\nFile3=Album/Three.flac\n"
        )
    rows = parse(source, format=format)
    assert [r.duration_ms for r in rows] == [8500, 0, 0]


def test_recording_hints_disambiguate_without_overriding_album_or_version() -> None:
    wanted = Entry(
        artist="Glass Orchard", title="First Light", isrc="ZZSHJ2600001", duration_ms=203500
    ).wanted()
    right = Track(
        id="right", artist="Glass Orchard", title="First Light", isrc="ZZSHJ2600001", duration=203
    )
    wrong = right.model_copy(update={"id": "wrong", "isrc": "ZZSHJ2600002"})
    unknown = right.model_copy(update={"id": "unknown", "isrc": ""})
    assert choose(wanted, [unknown, wrong, right]) == right
    assert choose(wanted, [wrong]) is None
    assert choose(wanted, [unknown]) == unknown
    assert choose(wanted, [right.model_copy(update={"title": "First Light (Live)"})]) is None
    assert choose({**wanted, "required_album": "Paper Garden"}, [right]) is None
    assert same_recording(
        wanted, right.model_copy(update={"isrc": "zz-shj-26-00001", "duration": 208})
    )
    assert not same_recording(wanted, right.model_copy(update={"duration": 209}))
    assert choose({**wanted, "isrc": ""}, [wrong, right]) is None
    assert (
        choose({**wanted, "isrc": ""}, [right, wrong.model_copy(update={"duration": 219})]) == right
    )


def test_native_multiple_isrcs_are_known_evidence_not_missing_metadata() -> None:
    data = {
        "id": "native",
        "artist": "Glass Orchard",
        "title": "First Light",
        "isrc": ["ZZSHJ2600001", "zz-shj-26-00002"],
    }
    song = native_track(data)
    wanted = Entry(artist="Glass Orchard", title="First Light", isrc="ZZSHJ2600002").wanted()
    assert choose(wanted, [song]) == song
    assert choose({**wanted, "isrc": "ZZSHJ2600003"}, [song]) is None
    other = song.model_copy(update={"id": "other", "isrc": "ZZSHJ2600002", "isrcs": []})
    assert choose(wanted, [song, other]) is not None
    assert not same_recording(song.model_dump(), native_track({**data, "isrc": ["ZZSHJ2600003"]}))
    assert same_recording(song.model_dump(), native_track({**data, "isrc": ["ZZSHJ2600002"]}))
    assert native_track({**data, "isrc": ["", "ZZSHJ2600002"]}).isrc == "ZZSHJ2600002"


def test_offered_remote_location_choices_keep_recording_constraints(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            service.client.call = AsyncMock(
                return_value={
                    "searchResult3": {
                        "song": [
                            {
                                "id": ident,
                                "artist": "Glass Orchard",
                                "title": "First Light",
                                "duration": 203,
                            }
                            for ident in ("one", "two")
                        ]
                    }
                }
            )
            ident = await service.imports.create(
                "owner",
                "remote-choice-key",
                "Remote references",
                "artist,title,location,isrc,duration_ms\n"
                "Glass Orchard,First Light,https://example.invalid/song,ZZSHJ2600001,202500\n",
            )
            run_id = (await service.repo.runs(ident))[0]["id"]
            await service.process(run_id)
            job = await service.repo.run(run_id)
            assert job["state"] == "preview", job["error"]
            assert job["data"]["rows"][0]["state"] == "ambiguous"
            reviewed = service.imports.review_key(job["data"])
            candidate = Track(id="one", artist="Glass Orchard", title="First Light", duration=203)
            for changed in (
                {"isrc": "ZZSHJ2600002"},
                {"duration": 208},
            ):
                service.native_track = AsyncMock(return_value=candidate.model_copy(update=changed))
                with pytest.raises(PlaylistError, match="Choose an offered recording"):
                    await service.imports.action(
                        "owner", ident, run_id, "choose", "", reviewed, entry="1", choice="one"
                    )
            service.native_track = AsyncMock(
                return_value=candidate.model_copy(update={"isrc": "ZZSHJ2600001"})
            )
            await service.imports.action(
                "owner", ident, run_id, "choose", "", reviewed, entry="1", choice="one"
            )
            await service.process(run_id)
            selected = (await service.repo.run(run_id))["data"]["selected"][0]
            assert selected["required_isrcs"] == ["ZZSHJ2600001"]
            assert selected["required_duration_ms"] == 202500

    anyio.run(run)


def test_empty_hints_preserve_existing_saved_rows() -> None:
    old_entry = {
        "artist": "Glass Orchard",
        "title": "First Light",
        "album": "",
        "version": "",
        "native_id": "",
        "locations": [],
    }
    assert Entry.model_validate(old_entry).model_dump() == old_entry
    track = Track(id="native", artist="Glass Orchard", title="First Light").model_dump()
    assert not {"isrcs", "required_isrcs", "required_duration_ms"} & track.keys()


def test_new_empty_column_controls_reopen_an_existing_import(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            old_mapping = {
                k: "" for k in ("artist", "title", "album", "version", "location", "native_id")
            }
            ident = await service.imports.create(
                "owner",
                "older-form-key",
                "Older form",
                "Glass Orchard — First Light",
                mapping=old_mapping,
            )
            new_mapping = {**old_mapping, "isrc": "", "duration": "", "duration_ms": ""}
            assert ident == await service.imports.create(
                "owner",
                "older-form-key",
                "Older form",
                "Glass Orchard — First Light",
                mapping=new_mapping,
            )

    anyio.run(run)


def test_import_evidence_and_constraints_survive_retries(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            service.client.call = AsyncMock(
                return_value={
                    "searchResult3": {
                        "song": [
                            {
                                "id": "unknown",
                                "artist": "Glass Orchard",
                                "title": "First Light",
                                "duration": 203,
                            },
                            {
                                "id": "right",
                                "artist": "Glass Orchard",
                                "title": "First Light",
                                "duration": 203,
                                "isrc": ["ZZSHJ2600001"],
                            },
                            {
                                "id": "wrong",
                                "artist": "Glass Orchard",
                                "title": "First Light",
                                "duration": 203,
                                "isrc": ["ZZSHJ2600002"],
                            },
                        ]
                    }
                }
            )
            source = (
                "artist,title,duration_ms,isrc\n"
                + "Glass Orchard,First Light,203500,ZZSHJ2600001\n" * 2
                + "Glass Orchard,First Light,219000,ZZSHJ2600001\n"
            )
            ident = await service.imports.create(
                "owner", "recording-key", "Recording evidence", source
            )
            run_id = (await service.repo.runs(ident))[0]["id"]
            await service.process(run_id)
            job = await service.repo.run(run_id)
            assert job["state"] == "preview", job["error"]
            assert [r["state"] for r in job["data"]["rows"]] == ["matched", "matched", "unresolved"]
            assert [s["id"] for s in job["data"]["selected"]] == ["right", "right"]
            selected = job["data"]["selected"][0]
            assert selected["required_isrcs"] == ["ZZSHJ2600001"]
            assert selected["required_duration_ms"] == 203500
            assert not same_recording(
                selected,
                Track(id="right", artist="Glass Orchard", title="First Light", duration=209),
            )
            evidence = json.loads((await service.imports.evidence("owner", ident))["input"])
            assert evidence["rows"][2]["duration_ms"] == 219000
            service.imports.inputs.clear()
            await service.imports.action("owner", ident, run_id, "retry", "")
            await service.process(run_id)
            retried = await service.repo.run(run_id)
            assert retried["data"]["selected"] == job["data"]["selected"]
            assert retried["data"]["rows"][2]["state"] == "unresolved"
            assert ident == await service.imports.create(
                "owner", "recording-key", "Recording evidence", source
            )

    anyio.run(run)
