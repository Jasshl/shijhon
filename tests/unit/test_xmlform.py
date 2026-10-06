"""The XML form of Subsonic answers (suites A, G and J in units): Navidrome's own XML read
into its JSON shape and written back byte for byte, Shijhon's changes written as Navidrome
writes the same types, Go's escaping and number format."""

from __future__ import annotations

from typing import Any

import pytest

from shijhon.catalog.model import CatalogArtist, CatalogRef, CatalogRelease, ReleaseKind
from shijhon.catalog.model import CatalogTrack as Track
from shijhon.proxy import xmlform
from shijhon.proxy.params import RestCall
from shijhon.proxy.responses import encoded
from shijhon.views.entries import album_child, album_entry, artist_entry, song_entry

NS = 'xmlns="http://subsonic.org/restapi"'
ENVELOPE = (
    f'<subsonic-response {NS} status="ok" version="1.16.1" type="navidrome"'
    ' serverVersion="0.64.2 (test)" openSubsonic="true">'
)
# Shaped like Navidrome 0.64.2's answers (invented names), with every kind of element.
SONG = (
    '<song id="s1" parent="al1" isDir="false" title="Paper &#34;Tides&#34; &amp; &lt;Rivers&gt;"'
    ' album="Amber Harbor" artist="Mara Vance" track="1" year="1995" genre="Rock"'
    ' coverArt="mf-s1_0" size="59836" contentType="audio/flac" suffix="flac"'
    ' starred="2025-03-14T09:26:53.589793-05:00" duration="3" bitRate="137"'
    ' path="Mara Vance/Amber Harbor/01-01 - Paper.flac" discNumber="1"'
    ' created="2025-03-14T09:26:53.238462-05:00" albumId="al1" artistId="ar1" type="music"'
    ' userRating="4" bpm="120" comment="line&#xA;two&#39;s" sortName="paper tides"'
    ' mediaType="song" musicBrainzId="mb-1" channelCount="2" samplingRate="44100"'
    ' bitDepth="16" displayArtist="Mara Vance" displayAlbumArtist="Mara Vance"'
    ' displayComposer="Tobin Quill">'
    '<isrc>ZZSHJ0000001</isrc><genres name="Rock"></genres>'
    '<replayGain trackGain="-6.5" trackPeak="0.98"></replayGain><moods>calm</moods>'
    '<artists id="ar1" name="Mara Vance"></artists>'
    '<albumArtists id="ar1" name="Mara Vance"></albumArtists>'
    '<contributors role="composer"><artist id="ar2" name="Tobin Quill"></artist></contributors>'
    "</song>"
)
ALBUM = (
    '<album id="al1" name="Amber Harbor" artist="Mara Vance" artistId="ar1"'
    ' coverArt="al-al1_0" songCount="2" duration="6"'
    ' created="2025-03-14T14:26:52.643383279Z" year="1995" genre="Rock"'
    ' sortName="amber harbor" displayArtist="Mara Vance">'
    '<genres name="Rock"></genres><discTitles disc="1" title="One"></discTitles>'
    '<releaseDate year="1995" month="3"></releaseDate><releaseTypes>album</releaseTypes>'
    '<recordLabels name="Velvet Records"></recordLabels>'
    '<artists id="ar1" name="Mara Vance"></artists>'
    f"{SONG}"
    '<song id="s2" parent="al1" isDir="false" title="Two" album="Amber Harbor"'
    ' duration="3" discNumber="1" track="2" mediaType="song">'
    '<artists id="ar1" name="Mara Vance"></artists></song>'
    "</album>"
)
GET_ALBUM = f"{ENVELOPE}{ALBUM}</subsonic-response>".encode()
ARTIST = (
    '<artist id="ar1" name="Mara Vance" coverArt="ar-ar1" albumCount="1" sortName="mara vance">'
    "<roles>albumartist</roles><roles>artist</roles></artist>"
)
SEARCH = f"{ENVELOPE}<searchResult3>{ARTIST}</searchResult3></subsonic-response>".encode()


def entries() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    ref = CatalogRef("demo", "900000001")
    track = Track(CatalogRef("demo", "900000002"), "Hollow Engines", "Neve Kestrel", 201_500,
                  1, 1, isrc="ZZSHJ0000009", album=ref)  # fmt: skip
    release = CatalogRelease(ref, "Quiet Meadow", "Neve Kestrel", ReleaseKind.ALBUM,
                               "2011-04-05", tracks=(track,), genres=("Pop",),
                               label="Salt Records")  # fmt: skip
    artist = CatalogArtist(CatalogRef("demo", "900000003"), "Neve Kestrel", "x/{w}x{h}")
    album = album_entry(release, songs=True)
    return album, song_entry(track, release), artist_entry(artist, [release])


def test_navidrome_s_answers_come_back_byte_for_byte() -> None:
    empty = f"{ENVELOPE}<topSongs></topSongs></subsonic-response>".encode()
    for body in (GET_ALBUM, SEARCH, empty):
        assert xmlform.dumps(xmlform.load(body)) == body


def test_an_answer_is_read_into_its_json_shape() -> None:
    response = xmlform.load(GET_ALBUM)["subsonic-response"]
    assert response["status"] == "ok" and response["openSubsonic"] is True
    album = response["album"]
    assert (album["songCount"], album["duration"], album["year"]) == (2, 6, 1995)
    # The keys JSON always carries, empty when XML leaves them out.
    assert (album["userRating"], album["isCompilation"], album["moods"]) == (0, False, [])
    assert album["originalReleaseDate"] == {}
    assert album["releaseDate"] == {"year": 1995, "month": 3}
    assert album["releaseTypes"] == ["album"]
    assert album["discTitles"] == [{"disc": 1, "title": "One"}]
    song = album["song"][0]
    assert song["title"] == 'Paper "Tides" & <Rivers>' and song["comment"] == "line\ntwo's"
    assert song["isrc"] == ["ZZSHJ0000001"] and song["userRating"] == 4
    assert song["replayGain"] == {"trackGain": -6.5, "trackPeak": 0.98}
    assert song["contributors"] == [
        {"role": "composer", "artist": {"id": "ar2", "name": "Tobin Quill"}}
    ]
    other = album["song"][1]
    assert other["replayGain"] == {} and other["genres"] == [] and other["isDir"] is False
    assert "year" not in other and "playCount" not in other  # JSON leaves them out too
    artist = xmlform.load(SEARCH)["subsonic-response"]["searchResult3"]["artist"][0]
    assert artist["roles"] == ["albumartist", "artist"] and artist["musicBrainzId"] == ""
    assert "album" not in xmlform.load(SEARCH)["subsonic-response"]["searchResult3"]


def test_changes_are_written_where_navidrome_would_write_them() -> None:
    document = xmlform.load(GET_ALBUM)
    album = document["subsonic-response"]["album"]
    album["songCount"], album["duration"] = 3, 207
    new = {
        "id": "sh.tr.demo.9",
        "isDir": False,
        "title": "Added",
        "duration": 201,
        "artists": [{"id": "ar1", "name": "Mara Vance"}],
        "replayGain": {},
        "bpm": 0,
    }
    album["song"] = [album["song"][1], new, album["song"][0]]
    out = xmlform.dumps(document).decode()
    assert 'songCount="3" duration="207" created=' in out  # in place
    assert out.index('id="s2"') < out.index('id="sh.tr.demo.9"') < out.index('id="s1"')
    assert SONG in out  # Navidrome's own entries as they came
    assert (
        '<song id="sh.tr.demo.9" isDir="false" title="Added" duration="201">'
        '<artists id="ar1" name="Mara Vance"></artists></song>'
    ) in out
    assert out.index("<artists") < out.index("<song ")  # the album's own elements first


def test_entries_added_where_navidrome_had_none_go_in_the_types_order() -> None:
    document = xmlform.load(SEARCH)
    result = document["subsonic-response"]["searchResult3"]
    album, song, _ = entries()
    result["song"] = [song]
    result["album"] = [{k: v for k, v in album.items() if k != "song"}]
    out = xmlform.dumps(document).decode()
    assert ARTIST in out
    assert out.index("<artist ") < out.index("<album ") < out.index("<song ")


def test_an_added_attribute_takes_its_place_in_the_types_order() -> None:
    document = xmlform.load(SEARCH)
    artist = document["subsonic-response"]["searchResult3"]["artist"][0]
    artist["starred"] = "2026-01-01T00:00:00Z"
    artist["albumCount"] = 2
    out = xmlform.dumps(document).decode()
    assert 'coverArt="ar-ar1" albumCount="2" starred="2026-01-01T00:00:00Z" sortName=' in out


def test_values_json_carries_empty_are_written_once_given() -> None:
    """Keys XML left out are filled in empty (as JSON carries them); filled in place
    afterwards, they are written in the types' order."""
    document = xmlform.load(GET_ALBUM)
    album = document["subsonic-response"]["album"]
    album["originalReleaseDate"]["year"] = 1990
    album["song"][1]["replayGain"]["trackGain"] = 0.0  # a pointer in Navidrome: zero is a value
    album["song"][1]["contributors"].append(
        {"role": "composer", "artist": {"id": "ar2", "name": "Tobin Quill"}}
    )
    out = xmlform.dumps(document).decode()
    assert '<discTitles disc="1" title="One"></discTitles><originalReleaseDate year="1990">' in out
    assert '<replayGain trackGain="0"></replayGain><artists id="ar1"' in out
    assert '<contributors role="composer"><artist id="ar2" name="Tobin Quill"></artist>' in out
    assert SONG in out


def test_replay_gain_as_navidrome_writes_it() -> None:
    fields = {"status": "ok", "version": "1.16.1"}
    song = {"id": "s", "isDir": False, "title": "T", "replayGain": {"trackGain": 0.0,
                                                                    "trackPeak": 0.98}}  # fmt: skip
    assert b'<replayGain trackGain="0" trackPeak="0.98"></replayGain>' in xmlform.answer(
        fields, {"song": song}
    )
    song["replayGain"] = {}
    assert b"replayGain" not in xmlform.answer(fields, {"song": song})


def test_shijhon_s_own_answers_are_written_as_navidrome_writes_them() -> None:
    fields = {"status": "ok", "version": "1.16.1", "type": "navidrome", "openSubsonic": True,
              "serverVersion": "0.64.2 (test)"}  # fmt: skip
    album, song, artist = entries()
    body = xmlform.answer(fields, {"album": album}).decode()
    assert body.startswith(ENVELOPE + '<album id="sh.al.demo.900000001" name="Quiet Meadow"')
    assert 'songCount="1" duration="201" created="2011-04-05T00:00:00Z" year="2011"' in body
    # Empty values are left out (XML's omitempty), as Navidrome leaves them out.
    assert 'userRating="' not in body and 'isCompilation="' not in body
    assert "<originalReleaseDate" not in body and "<replayGain" not in body
    assert '<releaseDate year="2011" month="4" day="5"></releaseDate>' in body
    assert '<recordLabels name="Salt Records"></recordLabels>' in body
    assert "<isrc>ZZSHJ0000009</isrc>" in body and 'isDir="false"' in body
    # The same entry as read back: the same data as its JSON form (zeros JSON carries
    # although Navidrome's own JSON leaves them out, such as a size, are left out).
    read = xmlform.load(body.encode())["subsonic-response"]["album"]
    assert same(read, album)
    assert same(xmlform.load(xmlform.answer(fields, {"song": song}))["subsonic-response"]["song"],
                song)  # fmt: skip
    shown = xmlform.load(xmlform.answer(fields, {"artist": artist}))["subsonic-response"]
    assert same(shown["artist"], artist)


def same(read: Any, written: Any) -> bool:
    """Equal, a missing key standing for an empty value."""
    if isinstance(read, dict) and isinstance(written, dict):
        keys = set(read) | set(written)
        return all(
            same(read[k], written[k]) if k in read and k in written
            else not (read.get(k) or written.get(k))
            for k in keys
        )  # fmt: skip
    if isinstance(read, list) and isinstance(written, list):
        return len(read) == len(written) and all(map(same, read, written))
    return type(read) is type(written) and read == written


def test_every_key_of_catalog_entries_has_its_xml_form() -> None:
    album, song, artist = entries()
    release = CatalogRelease(CatalogRef("demo", "5"), "Glass", "Ada Hale", ReleaseKind.EP,
                               "2020")  # fmt: skip
    assert xmlform.unknown(album, "AlbumWithSongsID3") == []
    assert xmlform.unknown(song, "Child") == []
    assert xmlform.unknown(artist, "ArtistWithAlbumsID3") == []
    assert xmlform.unknown(album_child(release), "Child") == []
    assert xmlform.unknown({**song, "surprise": 1}, "Child") == ["surprise"]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (5.0, "5"),
        (0.5, "0.5"),
        (-6.5, "-6.5"),
        (0.98, "0.98"),
        (100.0, "100"),
        (123456.0, "123456"),
        (1234567.0, "1.234567e+06"),
        (1e6, "1e+06"),
        (0.0001, "0.0001"),
        (0.00001, "1e-05"),
        (0.0, "0"),
    ],
)
def test_floats_as_go_writes_them(value: float, text: str) -> None:
    assert xmlform.go_float(value) == text


def test_escaping_as_go_escapes() -> None:
    assert xmlform.escape("a\"b'c&d<e>f\tg\nh\ri") == (
        "a&#34;b&#39;c&amp;d&lt;e&gt;f&#x9;g&#xA;h&#xD;i"
    )
    assert (
        xmlform.escape("x\x00y\ud800z") == "x\N{REPLACEMENT CHARACTER}y\N{REPLACEMENT CHARACTER}z"
    )
    assert xmlform.escape("Zoë Ångström 🎵") == "Zoë Ångström 🎵"


def test_what_is_not_a_subsonic_answer_is_refused() -> None:
    for body in (b"{}", b"<other></other>", b"<subsonic-response>", b""):
        with pytest.raises(ValueError):
            xmlform.load(body)


def test_jsonp_and_errors_as_navidrome_writes_them() -> None:
    def call(query: str) -> RestCall:
        return RestCall.build("getAlbum", "GET", b"/rest/getAlbum", query.encode(), [], None)

    server = {"version": "1.16.1", "type": "navidrome", "serverVersion": "0.64.2 (test)",
              "openSubsonic": True}  # fmt: skip
    kind, body = encoded(call("f=jsonp&callback=cb"), "ok", {"album": {"id": "a"}}, server)
    assert kind == b"application/javascript"
    assert body.startswith(b'cb({"subsonic-response":{"status":"ok","version":"1.16.1",')
    kind, body = encoded(call("f=jsonp&callback=1bad"), "ok", {}, server)
    assert kind == b"application/json" and b'"message":"invalid callback parameter"' in body
    kind, body = encoded(call(""), "failed", {"error": {"code": 70, "message": "x"}}, server)
    assert kind == b"application/xml"
    assert (
        body
        == (
            f"{ENVELOPE.replace('status="ok"', 'status="failed"')}"
            '<error code="70" message="x"></error></subsonic-response>'
        ).encode()
    )
