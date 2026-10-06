"""DASH manifests read into what to fetch (``delivery.mpd``): every standard static layout,
the choice among representations, and the refusals - each with a reason that names no
address; and the joined MP4's boxes (encryption, length) and a FLAC's sample count."""

from __future__ import annotations

import io
import struct

import pytest

from shijhon.delivery import mpd
from tests.harness.dash_fixtures import dash_audio

MANIFEST = "https://manifests.example/v1/track/abc/manifest.mpd?token=made-up"
NS = 'xmlns="urn:mpeg:dash:schema:mpd:2011"'


def manifest(body: str, *, attrs: str = 'type="static" mediaPresentationDuration="PT7S"') -> bytes:
    return f'<?xml version="1.0"?><MPD {NS} {attrs}>{body}</MPD>'.encode()


def period(*sets: str) -> str:
    return f'<Period id="0">{"".join(sets)}</Period>'


def audio_set(*reps: str, attrs: str = 'mimeType="audio/mp4" contentType="audio"') -> str:
    return f"<AdaptationSet {attrs}>{''.join(reps)}</AdaptationSet>"


def rep(ident: str, codecs: str, bandwidth: int, inner: str = "") -> str:
    attrs = f'id="{ident}" codecs="{codecs}" bandwidth="{bandwidth}"'
    return f"<Representation {attrs}>{inner}</Representation>"


def one(*reps: str, attrs: str = 'type="static" mediaPresentationDuration="PT7S"') -> bytes:
    """A manifest of one period with one audio set of ``reps``."""
    return manifest(period(audio_set(*reps)), attrs=attrs)


def flac(inner: str | None = None) -> str:
    return rep("1", "flac", 1, template() if inner is None else inner)


def template(timeline: str = '<S t="0" d="88200" r="2"/><S d="44100"/>', **extra: str) -> str:
    attrs = {
        "timescale": "44100",
        "initialization": "init-$RepresentationID$.mp4",
        "media": "seg-$RepresentationID$-$Number$.m4s",
        "startNumber": "1",
        **extra,
    }
    joined = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    inner = f"<SegmentTimeline>{timeline}</SegmentTimeline>" if timeline else ""
    return f"<SegmentTemplate {joined}>{inner}</SegmentTemplate>"


def test_the_measured_shape() -> None:
    """Three representations (HE-AAC 96k, AAC 320k, FLAC 823k), each a SegmentTemplate and
    SegmentTimeline with absolute addresses on another host than the manifest's, 77
    segments for 305.667 s, no BaseURL."""
    cdn = "https://cdn.example/audio/xyz"
    timeline = '<S t="0" d="176128" r="75"/><S d="74176"/>'

    def one(ident: str, codecs: str, bandwidth: int, rate: int) -> str:
        return rep(
            ident,
            codecs,
            bandwidth,
            template(
                timeline,
                timescale=str(rate),
                initialization=f"{cdn}/{ident}/init.mp4?k=1",
                media=f"{cdn}/{ident}/$Number$.mp4?k=1",
            ),
        )

    data = manifest(
        period(
            audio_set(
                one("a96", "mp4a.40.5", 96000, 22050),
                one("a320", "mp4a.40.2", 320000, 44100),
                one("f", "flac", 823000, 44100),
            )
        ),
        attrs='type="static" mediaPresentationDuration="PT5M5.667S" profiles="x"',
    )
    best = mpd.plan(data, MANIFEST)
    assert (best.codec, best.kind, best.bandwidth, best.lossless) == ("flac", "flac", 823000, True)
    assert best.layout == "SegmentTemplate+SegmentTimeline"
    assert len(best.media) == 77 and best.seconds == pytest.approx(305.667)
    assert best.init is not None and best.init.url == f"{cdn}/f/init.mp4?k=1"
    assert best.media[0].url == f"{cdn}/f/1.mp4?k=1"
    assert best.media[-1].url == f"{cdn}/f/77.mp4?k=1"
    assert best.offered == ("mp4a.40.5 96k", "mp4a.40.2 320k", "flac 823k")
    lossy = mpd.plan(data, MANIFEST, ("192", "320"))
    assert (lossy.codec, lossy.bandwidth, lossy.lossless) == ("mp4a.40.2", 320000, False)
    assert mpd.plan(data, MANIFEST, ("any", "128")).codec == "mp4a.40.5"


def chosen(quality: str, *reps: tuple[str, int]) -> tuple[str, int]:
    """The (codec, bandwidth) chosen in the range "from-to" of representations given as
    (codec, bandwidth)."""
    low, high = quality.split("-")
    found = mpd.plan(
        one(*(rep(str(n), c, b, template()) for n, (c, b) in enumerate(reps))),
        MANIFEST,
        (low, high),
    )
    return found.codec, found.bandwidth


AAC_ONLY = (("mp4a.40.5", 64000), ("mp4a.40.2", 256000), ("mp4a.40.2", 128000))
AAC_FLAC = (("mp4a.40.2", 320000), ("flac", 900000), ("mp4a.40.5", 96000))
MP3_AAC = (("mp4a.6B", 320000), ("mp4a.40.2", 192000), ("mp3", 128000))
NONE = None  # nothing in the range: refused


@pytest.mark.parametrize(
    "reps,quality,expected",
    [
        # Any to lossless: the best there is (the default).
        (AAC_ONLY, "any-lossless", ("mp4a.40.2", 256000)),
        (AAC_FLAC, "any-lossless", ("flac", 900000)),
        (MP3_AAC, "any-lossless", ("mp4a.6B", 320000)),
        # Lossless to lossless: lossless only.
        (AAC_ONLY, "lossless-lossless", NONE),
        (AAC_FLAC, "lossless-lossless", ("flac", 900000)),
        (MP3_AAC, "lossless-lossless", NONE),
        # 192 to 320: good lossy.
        (AAC_ONLY, "192-320", ("mp4a.40.2", 256000)),
        (AAC_FLAC, "192-320", ("mp4a.40.2", 320000)),
        (MP3_AAC, "192-320", ("mp4a.6B", 320000)),
        # Any to 128: small.
        (AAC_ONLY, "any-128", ("mp4a.40.2", 128000)),
        (AAC_FLAC, "any-128", ("mp4a.40.5", 96000)),
        (MP3_AAC, "any-128", ("mp3", 128000)),
        # A range with nothing in it.
        (AAC_FLAC, "128-256", NONE),
        (AAC_ONLY, "320-320", NONE),
        # Lossless whatever its stated rate (a muxer's nominal 128 kb/s).
        ((("flac", 128000), ("mp4a.40.2", 320000)), "any-lossless", ("flac", 128000)),
        ((("flac", 128000), ("mp4a.40.2", 320000)), "any-320", ("mp4a.40.2", 320000)),
    ],
)
def test_the_representation_chosen_in_a_quality_range(
    reps: tuple[tuple[str, int], ...], quality: str, expected: tuple[str, int] | None
) -> None:
    if expected is None:
        with pytest.raises(mpd.OutOfRange, match="no quality in the chosen range"):
            chosen(quality, *reps)
    else:
        assert chosen(quality, *reps) == expected


def test_a_bitrate_within_5_percent_of_an_end_counts_as_at_it() -> None:
    assert chosen("any-320", ("mp4a.40.2", 321588)) == ("mp4a.40.2", 321588)  # a peak
    assert chosen("any-320", ("mp4a.40.2", 336000)) == ("mp4a.40.2", 336000)
    with pytest.raises(mpd.OutOfRange):
        chosen("any-320", ("mp4a.40.2", 336001))
    assert chosen("192-lossless", ("mp4a.40.2", 182400)) == ("mp4a.40.2", 182400)
    with pytest.raises(mpd.OutOfRange):
        chosen("192-lossless", ("mp4a.40.2", 182399))


def test_at_the_same_bitrate_aac_lc_then_he_aac_then_mp3() -> None:
    same = (("mp3", 128000), ("mp4a.40.5", 128000), ("mp4a.40.2", 128000))
    assert chosen("any-lossless", *same) == ("mp4a.40.2", 128000)
    assert chosen("any-lossless", *same[:2]) == ("mp4a.40.5", 128000)
    assert chosen("any-lossless", ("alac", 900000), ("flac", 900000))[1] == 900000
    kinds = {c: mpd.plan(one(rep("1", c, 1, template())), MANIFEST).kind for c, _ in same}
    assert kinds == {"mp3": "mp3", "mp4a.40.5": "aac", "mp4a.40.2": "aac"}
    for codec in ("mp4a.40.34", "mp4a.69", "MP4A.6B"):  # MP3 in MP4, as muxers name it
        assert mpd.plan(one(rep("1", codec, 1, template())), MANIFEST).kind == "mp3"
    assert mpd.plan(one(rep("1", "alac", 1, template())), MANIFEST).kind == "alac"


def test_an_unreadable_bandwidth_is_none_and_codecs_are_told_as_text_only() -> None:
    found = mpd.plan(
        one(rep("1", "flac", 1, template()).replace('bandwidth="1"', 'bandwidth="lots"')),
        MANIFEST,
    )
    assert found.bandwidth == 0
    odd = one(rep("1", "&lt;b&gt; ec-3 http://x", 1, template()))
    with pytest.raises(mpd.Unsupported) as refused:
        mpd.plan(odd, MANIFEST)
    assert str(refused.value).endswith("(bec-3http//x)")


def test_every_audio_set_is_chosen_from_and_other_sets_are_not() -> None:
    video = audio_set(rep("v", "avc1.64001f", 5_000_000, template()), attrs='mimeType="video/mp4"')
    first = audio_set(rep("1", "mp4a.40.2", 128000, template()))
    second = audio_set(rep("2", "mp4a.40.2", 256000, template()))
    found = mpd.plan(manifest(period(video, first, second)), MANIFEST)
    assert found.bandwidth == 256000 and len(found.offered) == 2


def test_relative_addresses_resolve_against_the_manifest_s_own() -> None:
    found = mpd.plan(manifest(period(audio_set(rep("r1", "flac", 1, template())))), MANIFEST)
    assert found.init is not None
    assert found.init.url == "https://manifests.example/v1/track/abc/init-r1.mp4"
    assert [p.url for p in found.media] == [
        f"https://manifests.example/v1/track/abc/seg-r1-{n}.m4s" for n in (1, 2, 3, 4)
    ]


def test_base_urls_nest_and_the_first_of_several_is_used() -> None:
    body = (
        "<BaseURL>https://cdn.example/a/</BaseURL><BaseURL>https://other.example/</BaseURL>"
        '<Period id="0"><BaseURL>b/</BaseURL>'
        + audio_set(
            "<BaseURL>c/</BaseURL>" + rep("1", "flac", 1, "<BaseURL>file.mp4</BaseURL>"),
        )
        + "</Period>"
    )
    found = mpd.plan(manifest(body), MANIFEST)
    assert found.layout == "SegmentBase" and found.whole and found.init is None
    assert [p.url for p in found.media] == ["https://cdn.example/a/b/c/file.mp4"]
    absolute = manifest(period(audio_set(rep("1", "flac", 1, "<BaseURL>/x/y.mp4</BaseURL>"))))
    assert mpd.plan(absolute, MANIFEST).media[0].url == "https://manifests.example/x/y.mp4"


def test_template_tokens_widths_and_the_time_of_each_segment() -> None:
    media = "s/$RepresentationID$/$Bandwidth$/$Number%05d$-$Time$-$$.m4s"
    found = mpd.plan(
        manifest(
            period(
                audio_set(
                    rep(
                        "x7",
                        "mp4a.40.2",
                        256000,
                        template(
                            '<S t="100" d="10" r="1"/><S d="5"/>', media=media, startNumber="9"
                        ),
                    )
                )
            )
        ),
        MANIFEST,
    )
    names = [p.url.rsplit("/s/", 1)[1] for p in found.media]
    assert names == [
        "x7/256000/00009-100-$.m4s",
        "x7/256000/00010-110-$.m4s",
        "x7/256000/00011-120-$.m4s",
    ]
    assert found.seconds == 7.0  # the presentation's duration over the timeline's 25/44100 s


@pytest.mark.parametrize(
    "media", ["$Name$.m4s", "$Number%5d$.m4s", "$RepresentationID%02d$.m4s", "a$b.m4s"]
)
def test_template_tokens_it_does_not_know_are_refused(media: str) -> None:
    data = manifest(period(audio_set(rep("1", "flac", 1, template(media=media)))))
    with pytest.raises(mpd.Unsupported, match="template token"):
        mpd.plan(data, MANIFEST)


def test_a_template_with_a_duration_counts_its_segments() -> None:
    body = period(
        audio_set(
            rep(
                "1",
                "mp4a.40.2",
                1,
                template("", timescale="1000", duration="2000", media="$Number$-$Time$.m4s"),
            )
        )
    )
    found = mpd.plan(manifest(body), MANIFEST)  # 7 s: four segments, the last one short
    assert found.layout == "SegmentTemplate@duration"
    assert [p.url.rsplit("/", 1)[1] for p in found.media] == [
        "1-0.m4s",
        "2-2000.m4s",
        "3-4000.m4s",
        "4-6000.m4s",
    ]
    exact = manifest(body, attrs='type="static" mediaPresentationDuration="PT8S"')
    assert len(mpd.plan(exact, MANIFEST).media) == 4
    counted = audio_set(rep("1", "flac", 1, template("", duration="88200")))
    period_only = manifest(f'<Period id="0" duration="PT6S">{counted}</Period>', attrs="")
    assert len(mpd.plan(period_only, MANIFEST).media) == 3
    unknown = manifest(body, attrs='type="static"')
    with pytest.raises(mpd.Unsupported, match="cannot be counted"):
        mpd.plan(unknown, MANIFEST)


def test_a_segment_list_of_files_and_of_byte_ranges() -> None:
    files = (
        '<SegmentList timescale="1000" duration="2000"><Initialization sourceURL="i.mp4"/>'
        '<SegmentURL media="1.m4s"/><SegmentURL media="2.m4s"/></SegmentList>'
    )
    found = mpd.plan(manifest(period(audio_set(rep("1", "flac", 1, files)))), MANIFEST)
    assert found.layout == "SegmentList" and found.init is not None
    assert found.init.url.endswith("/abc/i.mp4") and found.init.first is None
    assert [p.url.rsplit("/", 1)[1] for p in found.media] == ["1.m4s", "2.m4s"]
    ranges = (
        "<BaseURL>one.mp4</BaseURL>"
        '<SegmentList><Initialization range="0-763"/>'
        '<SegmentURL mediaRange="764-999"/><SegmentURL mediaRange="1000-1500"/></SegmentList>'
    )
    found = mpd.plan(manifest(period(audio_set(rep("1", "flac", 1, ranges)))), MANIFEST)
    assert found.init is not None and (found.init.first, found.init.last) == (0, 763)
    assert [(p.first, p.last, p.size) for p in found.media] == [(764, 999, 236), (1000, 1500, 501)]
    assert {p.url for p in found.media} == {found.init.url} and not found.whole
    backwards = ranges.replace("764-999", "999-764")
    with pytest.raises(mpd.Unsupported, match="byte range"):
        mpd.plan(manifest(period(audio_set(rep("1", "flac", 1, backwards)))), MANIFEST)


def test_a_segment_base_is_one_file_and_may_name_an_init_file() -> None:
    base = (
        "<BaseURL>song.mp4</BaseURL>"
        '<SegmentBase indexRange="800-900"><Initialization range="0-799"/></SegmentBase>'
    )
    found = mpd.plan(manifest(period(audio_set(rep("1", "flac", 1, base)))), MANIFEST)
    assert found.layout == "SegmentBase" and found.whole and found.init is None
    assert found.media[0].url.endswith("/abc/song.mp4") and found.media[0].first is None
    separate = base.replace('range="0-799"', 'sourceURL="init.mp4"')
    found = mpd.plan(manifest(period(audio_set(rep("1", "flac", 1, separate)))), MANIFEST)
    assert found.init is not None and found.init.url.endswith("/abc/init.mp4") and found.whole


def test_a_layout_is_inherited_and_its_nearer_attributes_win() -> None:
    shared = template(timescale="1000", media="m-$RepresentationID$-$Number$.m4s")
    sets = (
        f'<AdaptationSet mimeType="audio/mp4" codecs="flac">{shared}'
        + rep("r", "flac", 1, '<SegmentTemplate startNumber="5"/>')
        + "</AdaptationSet>"
    )
    found = mpd.plan(manifest(period(sets)), MANIFEST)
    assert found.layout == "SegmentTemplate+SegmentTimeline"
    assert [p.url.rsplit("/", 1)[1] for p in found.media][:2] == ["m-r-5.m4s", "m-r-6.m4s"]


def test_the_ffmpeg_dash_muxer_s_layouts() -> None:
    for layout in ("timeline", "duration", "list", "ranges", "base"):
        folder = dash_audio("unit-layouts", 6, layout)
        found = mpd.plan((folder / "out.mpd").read_bytes(), "http://cdn.example/x/out.mpd")
        assert found.codec == "flac" and found.seconds == pytest.approx(6.0), layout
        assert len(found.offered) == 3, layout
        assert len(found.media) == (1 if layout == "base" else 3), layout


PROTECTION = '<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"/>'


LIST = "<SegmentList>" + '<SegmentURL media="a"/>' * 3001 + "</SegmentList>"
DYNAMIC = 'type="dynamic"'
REFUSED = {
    "live": (one(flac(), attrs=DYNAMIC), "live"),
    "two-periods": (manifest(period(audio_set(flac())) * 2), "several periods"),
    "no-period": (manifest(""), "no period"),
    "protected-mpd": (manifest(PROTECTION + period(audio_set(flac()))), "protected"),
    "protected-set": (one(PROTECTION + flac()), "protected"),
    "protected-representation": (one(flac(PROTECTION + template())), "protected"),
    "video-only": (
        manifest(period(audio_set(rep("1", "avc1", 1, template()), attrs='mimeType="video/mp4"'))),
        "no audio",
    ),
    "codecs": (
        one(rep("1", "ec-3", 1, template()), rep("2", "opus", 1, template())),
        r"no AAC, FLAC, ALAC or MP3 audio in MP4 \(ec-3, opus\)",
    ),
    "vorbis": (one(rep("1", "vorbis", 1, template())), "no AAC"),
    "webm": (manifest(period(audio_set(flac(), attrs='mimeType="audio/webm"'))), "no AAC"),
    "file-address": (one(flac("<BaseURL>file:///etc/passwd</BaseURL>")), "not http"),
    "timeline-too-long": (one(flac(template('<S d="1" r="3000"/>'))), "more than 3,000"),
    "open-ended": (one(flac(template('<S d="1" r="-1"/>'))), "open-ended"),
    "unreadable-number": (one(flac(template('<S d="1" r="x"/>'))), "cannot be read"),
    "list-too-long": (one(flac(LIST)), "more than 3,000"),
    "malformed": (b"<MPD", "unreadable"),
    "entities": (
        b'<?xml version="1.0"?><!DOCTYPE MPD [<!ENTITY a "a">]><MPD>&a;</MPD>',
        "unreadable",
    ),
    "not-mpd": (b"<html></html>", "not a DASH manifest"),
    "oversize": (manifest("<!--" + "x" * mpd.MAX_MANIFEST_BYTES + "-->"), "over 1 MiB"),
    "endless": (
        one(flac(template("", duration="1")), attrs=f'mediaPresentationDuration="PT{"9" * 400}S"'),
        "duration that cannot be read",
    ),
    "huge-timescale": (one(flac(template(timescale="1e308"))), "cannot be read"),
    "too-many-by-duration": (one(flac(template("", duration="1"))), "more than 3,000"),
    "no-address-of-its-own": (one(flac("<SegmentBase/>")), "without an address of its own"),
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_refused_manifests_say_why_and_name_no_address(case: str) -> None:
    data, reason = REFUSED[case]
    with pytest.raises(mpd.Unsupported, match=reason) as refused:
        mpd.plan(data, MANIFEST)
    assert "example" not in str(refused.value) and "/" not in str(refused.value)


# --- the joined MP4 ------------------------------------------------------------------


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def full(kind: bytes, payload: bytes, version: int = 0, flags: int = 0) -> bytes:
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def sample_entry(kind: bytes) -> bytes:
    return box(kind, bytes(28))


def init(entry: bytes = b"mp4a", *, extra: bytes = b"", mdhd_version: int = 0) -> bytes:
    mdhd = (
        full(b"mdhd", bytes(8) + struct.pack(">II", 44100, 0) + bytes(4))
        if mdhd_version == 0
        else full(b"mdhd", bytes(16) + struct.pack(">IQ", 48000, 96000) + bytes(4), version=1)
    )
    stsd = full(b"stsd", struct.pack(">I", 1) + sample_entry(entry))
    trak = box(b"trak", box(b"mdia", mdhd + box(b"minf", box(b"stbl", stsd))))
    trex = full(b"trex", struct.pack(">IIII", 1, 1, 1024, 0) + bytes(4))
    return box(b"ftyp", b"iso6") + box(b"moov", trak + box(b"mvex", trex) + extra)


def fragment(
    count: int, *, durations: list[int] | None = None, default: int | None = None
) -> bytes:
    tfhd = full(
        b"tfhd",
        struct.pack(">I", 1) + (struct.pack(">I", default) if default else b""),
        flags=0x8 if default else 0,
    )
    if durations is None:
        trun = full(b"trun", struct.pack(">I", count))
    else:
        trun = full(
            b"trun",
            struct.pack(">I", len(durations))
            + b"".join(struct.pack(">II", d, 100) for d in durations),
            flags=0x300,
        )
    return box(b"moof", box(b"traf", tfhd + trun)) + box(b"mdat", bytes(50))  # fmt: skip


def test_the_length_of_fragments_from_their_runs() -> None:
    data = init() + fragment(10) + fragment(3, default=2048) + fragment(0, durations=[1000, 500])
    found = mpd.inspect(data)
    assert not found.encrypted and found.timescale == 44100
    assert found.units == 10 * 1024 + 3 * 2048 + 1500
    assert found.seconds == pytest.approx(found.units / 44100)


def test_without_fragments_the_track_header_s_length() -> None:
    found = mpd.inspect(init(mdhd_version=1))
    assert (found.timescale, found.units, found.seconds) == (48000, 96000, 2.0)


@pytest.mark.parametrize(
    "data",
    [
        init(b"enca"),
        init(extra=full(b"pssh", bytes(20))),
        init() + box(b"moof", box(b"traf", full(b"senc", bytes(4)))),
    ],
    ids=["encrypted-entry", "key-system-header", "sample-encryption"],
)
def test_encrypted_audio_is_told_by_its_boxes(data: bytes) -> None:
    assert mpd.inspect(data).encrypted


def test_boxes_nested_too_deep_to_tell_are_refused_as_encrypted() -> None:
    nested = box(b"mdat")
    for _ in range(20):
        nested = box(b"moov", nested)
    assert mpd.inspect(nested).encrypted
    assert not mpd.inspect(init()).encrypted  # (the real nesting is four deep)


def test_a_box_that_does_not_fit_ends_the_walk() -> None:
    truncated = init()[:-5]
    found = mpd.inspect(truncated + fragment(4))
    assert not found.encrypted
    assert mpd.inspect(b"\x00\x00\x00\x01moov").units == 0  # a 64-bit size cut short


def test_the_muxer_s_segments_joined() -> None:
    folder = dash_audio("unit-joined", 6)
    joined = (folder / "init-stream2.m4s").read_bytes() + b"".join(
        p.read_bytes() for p in sorted(folder.glob("chunk-stream2-*.m4s"))
    )
    found = mpd.inspect(io.BytesIO(joined))
    assert not found.encrypted and found.units == 6 * 44100 and found.timescale == 44100


def test_a_flac_s_total_samples_are_read_and_written() -> None:
    head = (
        b"fLaC"
        + bytes([0, 0, 0, 34])
        + bytes(10)
        + (44100 << 44 | 2 << 41 | 15 << 36).to_bytes(8, "big")
    )
    assert mpd.flac_samples(head) == (44100, 0)
    assert mpd.flac_samples(mpd.with_samples(head, 264600)) == (44100, 264600)
    assert mpd.with_samples(head, 264600)[:18] == head[:18]
    assert mpd.flac_samples(b"ID3" + head[3:]) is None
    assert mpd.flac_samples(head[:4] + bytes([4]) + head[5:]) is None  # STREAMINFO not first


def test_the_segments_durations_what_an_index_of_them_says() -> None:
    """A timeline's durations and its first start; a duration's, the last one what is left
    (from the presentation time offset); a segment list's; none for one file."""
    timed = mpd.plan(one(flac(template('<S t="4410" d="88200" r="1"/><S d="44100"/>'))), MANIFEST)
    assert (timed.durations, timed.timescale, timed.earliest) == (
        (88200, 88200, 44100),
        44100,
        4410,
    )
    even = template("", timescale="1000", duration="2000", presentationTimeOffset="500")
    found = mpd.plan(one(rep("1", "flac", 1, even)), MANIFEST)  # 7 s
    assert (found.durations, found.timescale, found.earliest) == (
        (2000, 2000, 2000, 1000),
        1000,
        500,
    )
    files = (
        '<SegmentList timescale="1000" duration="2000"><Initialization sourceURL="i.mp4"/>'
        '<SegmentURL media="1.m4s"/><SegmentURL media="2.m4s"/></SegmentList>'
    )
    assert mpd.plan(one(rep("1", "flac", 1, files)), MANIFEST).durations == (2000, 2000)
    untold = one(rep("1", "flac", 1, files), attrs='type="static"')  # (the last one's length?)
    assert mpd.plan(untold, MANIFEST).durations is None
    listed = files.replace(
        '<SegmentURL media="1.m4s"/>',
        '<SegmentTimeline><S t="0" d="1500" r="1"/></SegmentTimeline><SegmentURL media="1.m4s"/>',
    )
    assert mpd.plan(one(rep("1", "flac", 1, listed)), MANIFEST).durations == (1500, 1500)
    base = '<BaseURL>song.mp4</BaseURL><SegmentBase><Initialization range="0-9"/></SegmentBase>'
    assert mpd.plan(one(rep("1", "flac", 1, base)), MANIFEST).durations is None


def test_a_timeline_with_a_gap_tells_no_durations() -> None:
    """(An index of contiguous segments would put the ones after the gap at other times.)"""
    gap = template('<S t="0" d="88200"/><S t="100000" d="88200"/>')
    found = mpd.plan(one(flac(gap)), MANIFEST)
    assert len(found.media) == 2 and found.durations is None
    jitter = template('<S t="0" d="88200"/><S t="88210" d="88200"/>')  # (10 ticks: no gap)
    assert mpd.plan(one(flac(jitter)), MANIFEST).durations == (88200, 88200)


def test_the_ffmpeg_dash_muxer_s_durations() -> None:
    for layout in ("timeline", "duration", "list", "ranges"):
        folder = dash_audio("unit-layouts", 6, layout)
        found = mpd.plan((folder / "out.mpd").read_bytes(), "http://cdn.example/x/out.mpd")
        assert found.durations is not None and len(found.durations) == len(found.media) == 3
        assert abs(sum(found.durations) / found.timescale - 6) < 0.05, layout
