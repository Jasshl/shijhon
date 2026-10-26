"""Ordinary local playlist/export compatibility using invented music and paths."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from shijhon.collection.contracts import Actor
from shijhon.dashboard.playlist_imports import Pages
from shijhon.playlists.import_formats import _csv_rows, detect, local_path, parse
from shijhon.playlists.model import PlaylistError, Track
from tests.unit.test_playlist_requests import service_at


@pytest.mark.parametrize("delimiter", ["\t", ","])
@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
@pytest.mark.parametrize("mode", ["auto", "filename", "explicit", "legacy_csv"])
@pytest.mark.parametrize("mapped", [False, True])
def test_table_boundary_empty_cells_preserve_all_recordings(
    delimiter: str, ending: str, mode: str, mapped: bool
) -> None:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter, lineterminator=ending or "\n")
    writer.writerows(
        [
            ["", "Artist", "Title", "Rating", "Album", "Unused"],
            ["", "Glass Orchard", "First Light", "", "Paper Garden", ""],
            ["", "Tideline", "Harbor", "", "", ""],
            ["", "Glass Orchard", "First Light", "", "Paper Garden", ""],
        ]
    )
    source = stream.getvalue() if ending else stream.getvalue().removesuffix("\n")
    rows = parse(
        source,
        filename=("ordinary.tsv" if delimiter == "\t" else "ordinary.csv")
        if mode == "filename"
        else "",
        format="csv" if mode == "explicit" else "auto",
        csv_file=mode == "legacy_csv",
        mapping={"artist": "Artist", "title": "Title", "album": "Album"} if mapped else None,
    )
    assert [(r.artist, r.title, r.album) for r in rows] == [
        ("Glass Orchard", "First Light", "Paper Garden"),
        ("Tideline", "Harbor", ""),
        ("Glass Orchard", "First Light", "Paper Garden"),
    ]


@pytest.mark.parametrize("delimiter", ["\t", ","])
@pytest.mark.parametrize("selected_tail", [True, False])
def test_table_bom_blank_padding_and_selected_or_ignored_empty_tail(
    delimiter: str, selected_tail: bool
) -> None:
    headers = ["Artist", "Title", "Album" if selected_tail else "Unused"]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerows(
        [headers, ["Glass Orchard", "First Light", ""], ["Glass Orchard", "First Light", ""]]
    )
    source = "\ufeff\r\n \t \r\n" + stream.getvalue() + " \t \r\n\r\n"
    rows = parse(source)
    assert [(r.artist, r.title, r.album) for r in rows] == [
        ("Glass Orchard", "First Light", "")
    ] * 2
    with pytest.raises(PlaylistError, match="byte budget"):
        parse(source, max_bytes=len(source.encode()) - 1)
    with pytest.raises(PlaylistError, match="entry budget"):
        parse(source, max_rows=1)


@pytest.mark.parametrize("delimiter", ["\t", ","])
@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_quoted_multiline_whitespace_metadata_passes_to_csv_reader_unchanged(
    delimiter: str,
    position: str,
) -> None:
    values = [
        ["Artist", "Title", "Unused", "Album"],
        ["Glass Orchard", "First Light", " \n\t \r\n quoted whitespace \n ", ""],
        ["Tideline", "Harbor", ' metadata with "quotes" and\t tabs ', ""],
    ]
    if position != "middle":
        for row in values:
            metadata = row.pop(2)
            row.insert(0 if position == "first" else len(row), metadata)
    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter=delimiter).writerows(values)
    source = stream.getvalue()
    assert list(_csv_rows(source)) == values
    assert [(r.artist, r.title, r.album) for r in parse(source)] == [
        ("Glass Orchard", "First Light", ""),
        ("Tideline", "Harbor", ""),
    ]


def test_uploaded_tsv_empty_tail_creates_exact_ordered_recording_review(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:

            async def resolve(connection: Any, wanted: dict[str, Any], **kwargs: Any) -> Track:
                return Track(
                    id="native-one" if wanted["title"].endswith("1") else "native-two",
                    artist=wanted["artist"],
                    title=wanted["title"],
                )

            service.resolver.resolve = AsyncMock(side_effect=resolve)
            dashboard = SimpleNamespace(
                services=SimpleNamespace(playlists=service),
                _see=lambda location: RedirectResponse(location, status_code=303),
                _render=AsyncMock(return_value=HTMLResponse("Import was refused", status_code=400)),
            )
            pages = Pages(dashboard)
            session = SimpleNamespace(user_id="owner")

            async def submit(request: Request) -> Response:
                request.state.form = await pages.read_form(request)
                return await pages.index(request, session)

            app = Starlette(routes=[Route("/import", submit, methods=["POST"])])
            source = "Artist\tTitle\tUnused\n" + "".join(
                f"Glass Orchard\tMorning Garden Song {number}\t\n" for number in (1, 2, 1)
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/import",
                    data={
                        "name": "Three ordered rows",
                        "request_key": "route-tsv-key",
                        "format": "auto",
                    },
                    files={"file": ("ordinary.tsv", source.encode(), "text/tab-separated-values")},
                )
            assert response.status_code == 303
            ident = response.headers["location"].rsplit("/", 1)[1]
            original = json.loads((await service.imports.evidence("owner", ident))["input"])
            assert original["source"] == source and original["filename"] == "ordinary.tsv"
            assert [row["title"] for row in original["rows"]] == [
                "Morning Garden Song 1",
                "Morning Garden Song 2",
                "Morning Garden Song 1",
            ]
            assert all(row["locations"] == [] for row in original["rows"])
            run_id = (await service.repo.runs(ident))[0]["id"]
            await service.process(run_id)
            review = await service.repo.run(run_id)
            assert review["state"] == "preview", review["error"]
            assert [row["id"] for row in review["data"]["selected"]] == [
                "native-one",
                "native-two",
                "native-one",
            ]
            assert service.resolver.resolve.call_count == 2

    anyio.run(run)


@pytest.mark.parametrize("filename", ["", "ordinary.txt"])
@pytest.mark.parametrize("separator", [" — ", " \u2013 ", " - ", "\t"])
def test_auto_text_keeps_slashes_dotted_titles_and_header_words(
    filename: str, separator: str
) -> None:
    pairs = [
        ("Glass/Orchard", "First Light"),
        ("Glass Orchard", "In/Out"),
        ("Glass Orchard", "Star.wav"),
        (r"Glass\Orchard", "Song.2"),
        ("Glass Orchard", "Name"),
        ("Glass Orchard", "Path"),
        ("Glass Orchard", "Title"),
        ("Glass Orchard", "Native ID"),
        ('Glass "Orchard', "First Light"),
    ]
    for artist, title in pairs:
        source = f"{artist}{separator}{title}\n"
        assert detect(source, filename) == "lines"
        row = parse(source, filename=filename)[0]
        assert (row.artist, row.title, row.locations) == (artist, title, [])
    # Different names can coexist without a first-row guess converting
    # later recording names into local-path requests. Repeated positions stay put.
    source = "\n\n".join(f"{artist}{separator}{title}" for artist, title in pairs + pairs[:2])
    rows = parse(source, filename=filename)
    assert [(row.artist, row.title) for row in rows] == pairs + pairs[:2]
    assert all(not row.locations for row in rows)


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
@pytest.mark.parametrize(
    "headers,values,field,expected",
    [
        (
            ["Track Name", "Artist Name(s)", "Rating"],
            ["Song.2", "Glass/Orchard", "5"],
            "title",
            "Song.2",
        ),
        (["Artist", "Title"], ["Glass Orchard", "In/Out"], "artist", "Glass Orchard"),
        (["Path", "Rating"], ["Album/One.flac", "5"], "locations", ["Album/One.flac"]),
        (["Native ID", "Rating"], ["native-one", "5"], "native_id", "native-one"),
        (
            ["Rating", "Path", "Unused"],
            ["5", "Album/One.flac", "x"],
            "locations",
            ["Album/One.flac"],
        ),
    ],
)
def test_auto_tables_require_recording_headers_without_losing_exports(
    delimiter: str, headers: list[str], values: list[str], field: str, expected: Any
) -> None:
    source = delimiter.join(headers) + "\n" + delimiter.join(values) + "\n"
    assert detect(source) == "csv"
    assert getattr(parse(source)[0], field) == expected


@pytest.mark.parametrize(
    "path",
    [
        "Album/One.flac",
        "One.flac",
        "file:///Music/Glass Orchard - One.flac",
        "https://example.invalid/Music/Glass Orchard - One.flac",
        r"C:\Music\Glass Orchard - One.flac",
        r"\\server\Music\Glass Orchard - One.flac",
        "/Music/Glass Orchard - One.flac",
        "../Music/Glass Orchard - One.flac",
    ],
)
def test_auto_paths_and_explicit_playlist_extensions_keep_file_identity(path: str) -> None:
    assert detect(path) == "m3u"
    assert parse(path)[0].locations == [path]
    assert parse("Glass Orchard - One.flac", filename="saved.m3u")[0].locations == [
        "Glass Orchard - One.flac"
    ]


def test_explicit_format_choices_resolve_ambiguous_text_or_table_input() -> None:
    assert parse("Artist\tTitle", format="lines")[0].artist == "Artist"
    assert parse("Glass/Orchard — One.flac", format="m3u")[0].locations == [
        "Glass/Orchard — One.flac"
    ]
    source = "Rating\tPath\n5\tAlbum/One.flac\n"
    assert parse(source, filename="export.tsv")[0].locations == ["Album/One.flac"]
    assert parse(source, format="csv")[0].locations == ["Album/One.flac"]
    with pytest.raises(PlaylistError, match="entry budget"):
        parse("Glass/Orchard — Song.2\nGlass Orchard\tPath", max_rows=1)
    with pytest.raises(PlaylistError, match="byte budget"):
        parse("Glass/Orchard — Song.2", max_bytes=8)


@pytest.mark.parametrize(
    "option",
    [
        "start-time=10",
        "stop-time=20",
        "run-time=10",
        ":Start-Time = 10",
        "\t:start-time=10",
    ],
)
def test_m3u_clip_instructions_are_not_silently_converted_to_whole_songs(option: str) -> None:
    source = (
        "#EXTM3U\n#EXTINF:120,Glass Orchard - Earlier Song\nAlbum/Earlier.flac\n"
        f"#EXTINF:10,Glass Orchard - First Light\n#EXTVLCOPT:{option}\nAlbum/One.flac\n"
    )
    for kwargs in ({}, {"filename": "clipped.m3u8"}, {"format": "m3u"}):
        with pytest.raises(PlaylistError, match="timed playback"):
            parse(source, **kwargs)


def test_m3u_metadata_comments_and_duration_hints_do_not_imply_clipping() -> None:
    source = (
        "#EXTM3U\n# Ordinary comment mentions start-time=10\n#PLAYLIST:Local songs\n"
        "#EXTALB:Paper Garden\n#EXTINF:10,Glass Orchard - First Light\n"
        "#EXTVLCOPT:network-caching=1000\n#EXTVLCOPT:meta-title=start-time\nAlbum/One.flac\n"
        "#EXTINF:-1,Glass Orchard - First Light\nAlbum/One.flac\n"
    )
    rows = parse(source)
    assert [(row.artist, row.title, row.locations) for row in rows] == [
        ("Glass Orchard", "First Light", ["Album/One.flac"])
    ] * 2


def test_m3u_clip_refusal_precedes_saved_review_but_metadata_import_keeps_original(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            service.imports.library_reader = SimpleNamespace(
                authorize=AsyncMock(return_value=Actor("owner", frozenset({"1"}))),
                songs=AsyncMock(return_value=[{"id": "native-one", "path": "Album/One.flac"}]),
            )
            service.native_track = AsyncMock(
                return_value=Track(
                    id="native-one", artist="Glass Orchard", title="First Light", duration=120
                )
            )
            source = "#EXTM3U\n#EXTINF:10,Glass Orchard - First Light\nAlbum/One.flac\n"
            clipped = source.replace(
                "Album/One.flac",
                "#EXTVLCOPT:start-time=10\n#EXTVLCOPT:stop-time=20\nAlbum/One.flac",
            )
            with pytest.raises(PlaylistError, match="timed playback"):
                await service.imports.create("owner", "clip-refusal-key", "Clipped", clipped)
            for query in (
                "SELECT id FROM playlist_imports",
                "SELECT id FROM playlist_mixes",
                "SELECT id FROM playlist_runs",
            ):
                assert not await service.repo.store.fetchall(query)
            service.native_track.assert_not_called()
            ident = await service.imports.create("owner", "metadata-key", "Metadata only", source)
            run_id = (await service.repo.runs(ident))[0]["id"]
            await service.process(run_id)
            result = await service.repo.run(run_id)
            assert result["state"] == "preview", result["error"]
            assert result["data"]["selected"][0]["duration"] == 120
            assert (
                json.loads((await service.imports.evidence("owner", ident))["input"])["source"]
                == source
            )

    anyio.run(run)


def test_auto_mixed_name_pairs_create_recording_review_without_path_matching(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:

            async def resolve(connection: Any, wanted: dict[str, Any], **kwargs: Any) -> Track:
                return Track(
                    id=f"native-{wanted['title']}", artist=wanted["artist"], title=wanted["title"]
                )

            service.resolver.resolve = AsyncMock(side_effect=resolve)
            source = (
                "Glass/Orchard — Song.2\nGlass Orchard\tPath\n"
                "Glass Orchard - In/Out\nGlass/Orchard — Song.2"
            )
            ident = await service.imports.create("owner", "mixed-text-key", "Mixed names", source)
            evidence = json.loads((await service.imports.evidence("owner", ident))["input"])
            assert evidence["source"] == source
            assert all(not row["locations"] for row in evidence["rows"])
            run_id = (await service.repo.runs(ident))[0]["id"]
            await service.process(run_id)
            result = await service.repo.run(run_id)
            assert result["state"] == "preview", result["error"]
            assert [(row["artist"], row["title"]) for row in result["data"]["selected"]] == [
                ("Glass/Orchard", "Song.2"),
                ("Glass Orchard", "Path"),
                ("Glass Orchard", "In/Out"),
                ("Glass/Orchard", "Song.2"),
            ]
            assert service.resolver.resolve.call_count == 3

    anyio.run(run)


def test_common_csv_exports_extra_columns_and_explicit_mapping() -> None:
    rows = parse(
        "Track Name,Artist Name(s),Album Name,Added At,Track URI\n"
        "First Light,Glass Orchard,Paper Garden,2026-01-01,https://example.invalid/track/1\n"
        "First Light,Glass Orchard,Paper Garden,2026-01-02,https://example.invalid/track/1\n",
        filename="export.csv",
    )
    assert [(r.artist, r.title, r.album) for r in rows] == [
        ("Glass Orchard", "First Light", "Paper Garden"),
    ] * 2
    assert rows[0].locations == ["https://example.invalid/track/1"]
    assert (
        parse(
            "Who;Recording;Rating\nGlass Orchard;First Light;5\n",
            format="csv",
            mapping={"artist": "Who", "title": "Recording"},
        )[0].title
        == "First Light"
    )
    assert (
        parse("Artist\tName\tLength\nGlass Orchard\tFirst Light\t123\n", filename="export.tsv")[
            0
        ].artist
        == "Glass Orchard"
    )
    assert parse("artist,title,id\nA,One,unrelated-service-id\n", csv_file=True)[0].native_id == ""
    assert (
        parse("id,unused\nnative-one,x\n", format="csv", mapping={"native_id": "id"})[0].native_id
        == "native-one"
    )
    with pytest.raises(PlaylistError, match="Several columns"):
        parse("artist,title,name\nA,One,Other\n", csv_file=True)
    assert (
        parse("artist,title,name\nA,One,Other\n", csv_file=True, mapping={"title": "title"})[
            0
        ].title
        == "One"
    )


@pytest.mark.parametrize("extras", ["Artwork URL,Artwork_URL", "画像,評価", "extra,extra"])
@pytest.mark.parametrize("mapped", [False, True])
def test_unused_csv_header_collisions_do_not_block_recording_columns(
    extras: str, mapped: bool
) -> None:
    rows = parse(
        f"artist,title,{extras}\nGlass Orchard,First Light,a,b\n",
        format="csv",
        mapping={"artist": "artist", "title": "title"} if mapped else {},
    )
    assert [(row.artist, row.title) for row in rows] == [("Glass Orchard", "First Light")]


def test_csv_mapping_disambiguates_normalized_selected_headers_but_not_exact_duplicates() -> None:
    source = "artist,title,Track Name,Track_Name\nA,One,Two,Three\n"
    with pytest.raises(PlaylistError, match="Several columns"):
        parse(source, format="csv")
    assert parse(source, format="csv", mapping={"title": "Track_Name"})[0].title == "Three"
    with pytest.raises(PlaylistError, match="Several columns"):
        parse("artist,title,title\nA,One,Two\n", format="csv", mapping={"title": "title"})


@pytest.mark.parametrize(
    "filename,source",
    [
        (
            "music.m3u",
            "#EXTM3U\n#EXTINF:-1,Glass Orchard - First Light\nAlbum/01.flac\nAlbum/01.flac\n",
        ),
        ("music.m3u8", "Album/01.flac\nAlbum/01.flac\n"),
        (
            "music.pls",
            "[playlist]\nFile2=Album/01.flac\nTitle2=Glass Orchard - First Light\n"
            "File1=Album/01.flac\nNumberOfEntries=2\nVersion=2\n",
        ),
        (
            "music.xspf",
            '<playlist xmlns="http://xspf.org/ns/0/" version="1"><trackList>'
            "<track><location>Album/01.flac</location><creator>Glass Orchard</creator>"
            "<title>First Light</title></track><track><location>Album/01.flac</location></track>"
            "</trackList></playlist>",
        ),
        (
            "music.jspf",
            json.dumps(
                {
                    "playlist": {
                        "track": [{"location": ["Album/01.flac"]}, {"location": ["Album/01.flac"]}]
                    }
                }
            ),
        ),
        (
            "music.asx",
            '<ASX version="3.0"><ENTRY><REF href="Album/01.flac"/></ENTRY>'
            '<ENTRY><REF href="Album/01.flac"/></ENTRY></ASX>',
        ),
        (
            "music.wpl",
            '<?wpl version="1.0"?><smil><body><seq><media src="Album/01.flac"/>'
            '<media src="Album/01.flac"/></seq></body></smil>',
        ),
    ],
)
def test_local_formats_keep_order_and_repeats(filename: str, source: str) -> None:
    rows = parse(source, filename=filename)
    assert [r.locations for r in rows] == [["Album/01.flac"], ["Album/01.flac"]]


def test_xspf_alternates_namespaces_relative_base_and_native_id() -> None:
    rows = parse(
        '<playlist xmlns="http://xspf.org/ns/0/" xml:base="file:///Music/">'
        "<trackList><track><location>Album/One.flac</location>"
        "<location>https://example.invalid/one</location><identifier>navidrome:native-one</identifier>"
        "<creator>Glass Orchard</creator><title>First Light</title></track></trackList></playlist>"
    )
    assert rows[0].locations == ["file:///Music/Album/One.flac", "https://example.invalid/one"]
    assert rows[0].native_id == "native-one"


@pytest.mark.parametrize(
    "source,format",
    [
        (
            '<!DOCTYPE playlist [<!ENTITY x SYSTEM "file:///secret">]><playlist><trackList>'
            "<track><title>&x;</title></track></trackList></playlist>",
            "xspf",
        ),
        ("[playlist]\nFile1=One.flac\nFile3=Three.flac\n", "pls"),
        ("[playlist]\nFile1=One.flac\nNumberOfEntries=2\n", "pls"),
        ("#EXTM3U\n#EXT-X-TARGETDURATION:10\nsegment.ts\n", "m3u"),
        ("<playlist><trackList><track/></trackList></playlist>", "xspf"),
        ('{"playlist":{"track":[{}]}}', "jspf"),
    ],
)
def test_malformed_or_executable_streaming_documents_reject_as_whole(
    source: str, format: str
) -> None:
    with pytest.raises(PlaylistError):
        parse(source, format=format)


def test_large_inputs_have_configurable_budgets_instead_of_1000_ceiling() -> None:
    assert len(parse("A — One\n" * 10_001)) == 10_001
    assert len(parse("A — One\n" * 1001, max_rows=1001)) == 1001
    with pytest.raises(PlaylistError, match="entry budget"):
        parse("A — One\n" * 1002, max_rows=1001)
    with pytest.raises(PlaylistError, match="byte budget"):
        parse("A — One", max_bytes=5)


@pytest.mark.parametrize(
    "location,expected",
    [
        (r"C:\Music\Glass Orchard\One.flac", "Music/Glass Orchard/One.flac"),
        ("file:///Music/Glass%20Orchard/One.flac", "Music/Glass Orchard/One.flac"),
        ("../Glass Orchard/./One.flac", "Glass Orchard/One.flac"),
        ("https://example.invalid/music/One.flac", ""),
        ("file://remote.invalid/music/One.flac", ""),
        ("data:audio/flac,hello", ""),
    ],
)
def test_locations_are_lexical_data_only(location: str, expected: str) -> None:
    assert local_path(location) == expected


def test_finite_xml_repeat_groups_preserve_entry_order_and_resource_budget() -> None:
    asx = (
        '<ASX><REPEAT COUNT="2"><ENTRY><REF HREF="A.flac"/></ENTRY>'
        '<ENTRY><REF HREF="B.flac"/></ENTRY></REPEAT></ASX>'
    )
    wpl = (
        '<smil><body><seq repeatCount="2"><media src="A.flac"/>'
        '<media src="B.flac"/></seq></body></smil>'
    )
    for format, source in (("asx", asx), ("wpl", wpl)):
        assert [r.locations[0] for r in parse(source, format=format)] == ["A.flac", "B.flac"] * 2
        with pytest.raises(PlaylistError, match="entry budget"):
            parse(source, format=format, max_rows=3)
    with pytest.raises(PlaylistError):
        parse('<ASX><REPEAT><ENTRY><REF HREF="A.flac"/></ENTRY></REPEAT></ASX>', format="asx")


def test_asx_zero_count_and_wpl_integral_decimal_media_repeats_keep_finite_tracks() -> None:
    rows = parse(
        '<ASX><REPEAT COUNT="0"><ENTRY><REF HREF="A.flac"/></ENTRY></REPEAT></ASX>',
        format="asx",
    )
    assert [row.locations for row in rows] == [["A.flac"]]
    rows = parse(
        '<smil><body><seq repeatCount="2"><media src="A.flac" repeatCount="2.0"/>'
        '<media src="B.flac"/></seq></body></smil>',
        format="wpl",
    )
    assert [row.locations for row in rows] == [["A.flac"], ["A.flac"], ["B.flac"]] * 2
    for count in ("0.5", "indefinite", "NaN", "Infinity"):
        with pytest.raises(PlaylistError):
            parse(
                f'<smil><body><media src="A.flac" repeatCount="{count}"/></body></smil>',
                format="wpl",
            )


def test_nested_xspf_xml_bases_and_malformed_jspf_have_safe_results() -> None:
    rows = parse(
        '<playlist xmlns="http://xspf.org/ns/0/" xml:base="file:///Music/">'
        '<trackList xml:base="Album/"><track xml:base="Disc/">'
        '<location xml:base="../">One.flac</location></track></trackList></playlist>'
    )
    assert rows[0].locations == ["file:///Music/Album/One.flac"]
    with pytest.raises(PlaylistError):
        parse('{"playlist":{"track":["not a track object"]}}', format="jspf")


def test_asx_scoped_bases_alternates_metadata_and_repeats_are_preserved() -> None:
    rows = parse(
        '<ASX><BASE HREF="https://example.invalid/music"/>'
        "<ENTRY><TITLE>Glass Orchard - First Light</TITLE>"
        '<REF HREF="One.flac"/><REF HREF="https://other.invalid/One.flac"/></ENTRY>'
        '<REPEAT COUNT="2"><ENTRY><BASE HREF="file:///Music/Album"/>'
        '<REF HREF="One.flac"/></ENTRY>'
        '<ENTRY><BASE HREF="../other/"/><REF HREF="Two.flac"/></ENTRY></REPEAT>'
        '<ENTRY><REF HREF="Three.flac"/></ENTRY></ASX>',
        format="asx",
    )
    assert [(rows[0].artist, rows[0].title)] == [("Glass Orchard", "First Light")]
    assert [row.locations for row in rows] == [
        ["https://example.invalid/music/One.flac", "https://other.invalid/One.flac"],
        ["file:///Music/Album/One.flac"],
        ["https://example.invalid/other/Two.flac"],
        ["file:///Music/Album/One.flac"],
        ["https://example.invalid/other/Two.flac"],
        ["https://example.invalid/music/Three.flac"],
    ]


@pytest.mark.parametrize("scheme", ["https", "mms", "rtsp", "custom"])
@pytest.mark.parametrize("format", ["asx", "xspf", "wpl"])
def test_xml_nested_bases_keep_remote_references_remote(format: str, scheme: str) -> None:
    source = {
        "asx": f'<ASX xml:base="{scheme}://example.invalid/Music/"><REPEAT COUNT="2">'
        '<ENTRY xml:base="Album/"><REF xml:base="Disc/" HREF="../One.flac"/>'
        "</ENTRY></REPEAT></ASX>",
        "xspf": f'<playlist xml:base="{scheme}://example.invalid/Music/">'
        '<trackList xml:base="Album/"><track><location xml:base="Disc/">../One.flac</location>'
        "</track></trackList></playlist>",
        "wpl": f'<smil xml:base="{scheme}://example.invalid/Music/"><body><seq xml:base="Album/">'
        '<media xml:base="Disc/" src="../One.flac" repeatCount="2"/></seq></body></smil>',
    }[format]
    rows = parse(source, format=format)
    assert [r.locations for r in rows] == [[f"{scheme}://example.invalid/Music/Album/One.flac"]] * (
        1 if format == "xspf" else 2
    )
    assert all(not local_path(v) for row in rows for v in row.locations)


@pytest.mark.parametrize(
    "element", ["STARTTIME", "DURATION", "PREVIEWDURATION", "STARTMARKER", "ENDMARKER"]
)
@pytest.mark.parametrize("inside_ref", [False, True])
def test_asx_timing_at_entry_and_reference_rejects_whole_document(
    element: str, inside_ref: bool
) -> None:
    instruction = f'<{element} VALUE="00:00:10"/>'
    entry = (
        f'<ENTRY><REF HREF="One.flac">{instruction}</REF></ENTRY>'
        if inside_ref
        else f'<ENTRY><REF HREF="One.flac"/>{instruction}</ENTRY>'
    )
    with pytest.raises(PlaylistError, match="timed playback"):
        parse(f'<ASX><ENTRY><REF HREF="First.flac"/></ENTRY>{entry}</ASX>', format="asx")


@pytest.mark.parametrize(
    "entry,message",
    [
        ('<ENTRYREF HREF="other.asx"/>', "Referenced playlists"),
        ('<EVENT NAME="next"><ENTRY><REF HREF="One.flac"/></ENTRY></EVENT>', "conditional"),
    ],
)
def test_asx_does_not_treat_nested_playlist_or_event_as_ordinary_track(
    entry: str, message: str
) -> None:
    with pytest.raises(PlaylistError, match=message):
        parse(f'<ASX><ENTRY><REF HREF="First.flac"/></ENTRY>{entry}</ASX>', format="asx")


@pytest.mark.parametrize(
    "attribute", ["begin", "end", "dur", "clipBegin", "clip-end", "repeatDur", "min", "max"]
)
def test_wpl_does_not_drop_clip_or_timing_instructions(attribute: str) -> None:
    with pytest.raises(PlaylistError, match="timed playback"):
        parse(
            f'<smil><body><seq><media src="One.flac" {attribute}="1s"/></seq></body></smil>',
            format="wpl",
        )


def test_xspf_duration_is_recording_metadata_not_playback_clipping() -> None:
    rows = parse(
        "<playlist><trackList><track><location>Album/One.flac</location><duration>123000</duration>"
        "<creator>Glass Orchard</creator><title>First Light</title><album>Paper Garden</album>"
        "</track></trackList></playlist>",
        format="xspf",
    )
    assert (rows[0].artist, rows[0].title, rows[0].album) == (
        "Glass Orchard",
        "First Light",
        "Paper Garden",
    )


def test_ordinary_unicode_spaces_and_marks_are_music_text() -> None:
    """No-break, narrow no-break and ideographic spaces and invisible marks occur in
    ordinary exported titles; controls and bidirectional overrides do not import."""
    nbsp, narrow, wide, zero, ltr = (chr(c) for c in (0xA0, 0x202F, 0x3000, 0x200B, 0x200E))
    source = "\n".join(
        [
            f"Claire{nbsp}Dumont — Pourquoi{narrow}?",
            f"Hoshino{wide}Kaze — 夜明け{wide}の歌",
            f"Lumen{zero}Fields — {ltr}Night Drive",
        ]
    )
    rows = parse(source, format="lines")
    assert [(r.artist, r.title) for r in rows] == [
        (f"Claire{nbsp}Dumont", f"Pourquoi{narrow}?"),
        (f"Hoshino{wide}Kaze", f"夜明け{wide}の歌"),
        (f"Lumen{zero}Fields", f"{ltr}Night Drive"),
    ]
    # Matching folds them away: the same recording as the plain spelling.
    assert rows[0].suggestion() is not None
    for unsafe in (chr(0x202E), chr(0x2066), chr(0x07)):
        with pytest.raises(PlaylistError):
            parse(f"Lumen Fields — Night{unsafe}Drive", format="lines")
