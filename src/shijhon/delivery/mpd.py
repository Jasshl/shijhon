"""DASH manifests (MPD) and the MP4 they lead to, read without fetching anything.

An add-on's link may be a DASH manifest rather than a file: a list of segments that,
joined in order, are one fragmented MP4 (``delivery.dash`` fetches and joins them). This
module reads the manifest into the addresses to fetch, and checks the joined bytes.

Played: a static manifest with one Period and audio in MP4 - AAC, FLAC, ALAC or MP3 - in
any of the standard layouts:

- ``SegmentTemplate`` with a ``SegmentTimeline`` (the tokens ``$Number$``, ``$Time$``,
  ``$RepresentationID$`` and ``$Bandwidth$``, ``%0Nd`` widths, ``r`` repeats);
- ``SegmentTemplate`` with ``@duration`` (as many segments as the period's or the
  presentation's duration needs);
- ``SegmentList`` (``SegmentURL@media``, and ``@mediaRange`` as a byte range);
- ``SegmentBase``, or none of them: one file, fetched whole.

``BaseURL`` elements nest (MPD, Period, AdaptationSet, Representation; the first of several
is used) and resolve against the manifest's own address after its redirects. Of all the
audio representations, the highest-ranked within the quality range asked for: lossless
(FLAC, ALAC) above every lossy bitrate, lossy ones by their bandwidth - within 5 % of a
bound counts as at it, as manifests state the peak (321,588 bit/s is 320 kbit/s) - and at
the same bandwidth AAC-LC, then other AAC (HE-AAC), then MP3. None within the range: the
manifest is not played (``OutOfRange``).

Refused, with a short reason that names no address: a live (dynamic) manifest, several
Periods, any ``ContentProtection``, another codec (Opus, Vorbis, AC-3...) or container
(WebM), a document type declaration
(entities), a manifest over 1 MiB or with more than 3,000 segments. Encrypted audio is
refused too when its boxes say so (``inspect``): nothing is ever decrypted.
"""

from __future__ import annotations

import io
import itertools
import math
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, ParseError

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SEGMENTS = 3000
LOSSLESS = ("flac", "alac")
MAX_SECONDS = 7 * 86400.0  # a presentation longer than a week is no song
_SAFE = re.compile(r"[^A-Za-z0-9._/+-]")  # what a codec or type may say in a reason
# The bounds of a quality range, from the lowest: kbit/s, and lossless above them all.
QUALITIES = ("any", "128", "192", "256", "320", "lossless")
BOUND_TOLERANCE = 0.05
# AAC's object types (RFC 6381: "mp4a.40.<type>"): Main, LC, LTP, HE (SBR), HE v2 (PS).
_AAC = {"mp4a.40.1", "mp4a.40.2", "mp4a.40.4", "mp4a.40.5", "mp4a.40.29"}
# MP3 in MP4: by its object type indication (MPEG-1 or MPEG-2 audio), or as MPEG-4 audio
# of layer 3.
_MP3 = {"mp3", "mp4a.6b", "mp4a.69", "mp4a.40.34"}
_DURATION = re.compile(
    r"P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?"
)
_TOKEN = re.compile(r"\$(RepresentationID|Number|Time|Bandwidth)?(%0(\d{1,2})d)?\$")
_RANGE = re.compile(r"(\d+)-(\d+)")
_LAYOUTS = ("SegmentTemplate", "SegmentList", "SegmentBase")


class Unsupported(Exception):
    """A manifest (or audio) Shijhon does not play; the message says why."""


class OutOfRange(Unsupported):
    """No representation of the manifest is within the quality range asked for."""


@dataclass(frozen=True)
class Part:
    """One request of a join: an address, and a byte range of it when only that is meant."""

    url: str = field(repr=False)
    first: int | None = None
    last: int | None = None

    @property
    def size(self) -> int | None:
        return None if self.first is None or self.last is None else self.last - self.first + 1


@dataclass(frozen=True)
class Plan:
    """What to fetch for the chosen representation, and what the manifest offered."""

    codec: str  # as the manifest names it ("flac", "mp4a.40.2")
    kind: str  # "flac", "alac", "aac" or "mp3"
    bandwidth: int  # bits a second
    init: Part | None
    media: tuple[Part, ...]
    seconds: float | None  # the presentation's length, when the manifest tells it
    layout: str  # e.g. "SegmentTemplate+SegmentTimeline"
    offered: tuple[str, ...]  # every audio representation, e.g. "flac 823k" (for the log)
    # Each media segment's duration in ``timescale`` (None: the manifest does not tell),
    # and the first one's start (its media time): what an index of them says.
    durations: tuple[int, ...] | None = None
    timescale: int = 1
    earliest: int = 0

    @property
    def lossless(self) -> bool:
        return self.kind in LOSSLESS

    @property
    def whole(self) -> bool:
        """The media is one file, fetched whole (SegmentBase): it may be large, so it is
        written as it comes rather than held in memory."""
        return len(self.media) == 1 and self.media[0].first is None


@dataclass
class _Rep:
    period: Element
    aset: Element
    rep: Element
    codec: str
    kind: str | None
    bandwidth: int


def plan(data: bytes, url: str, quality: tuple[str, str] = ("any", "lossless")) -> Plan:
    """The segments to fetch for the audio representation chosen of the manifest ``data``,
    which was read from ``url`` (its address after redirects); ``quality``: the range
    chosen from, two of ``QUALITIES``. Raises ``Unsupported`` (``OutOfRange``)."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise Unsupported("a manifest over 1 MiB")
    try:
        root = fromstring(data, forbid_dtd=True)
    except (DefusedXmlException, ParseError, ValueError):
        raise Unsupported("an unreadable manifest (or one with a document type)") from None
    if _name(root) != "MPD":
        raise Unsupported("not a DASH manifest")
    if (root.get("type") or "static").strip() != "static":
        raise Unsupported("a live manifest")
    if any(_name(element) == "ContentProtection" for element in root.iter()):
        raise Unsupported("protected audio (ContentProtection)")
    periods = _children(root, "Period")
    if len(periods) != 1:
        raise Unsupported("several periods" if periods else "no period")
    period = periods[0]
    seconds = _seconds(root.get("mediaPresentationDuration")) or _seconds(period.get("duration"))
    reps: list[_Rep] = []
    for aset in _children(period, "AdaptationSet"):
        for rep in _children(aset, "Representation"):
            mime = (rep.get("mimeType") or aset.get("mimeType") or "").strip().lower()
            content = (aset.get("contentType") or rep.get("contentType") or "").strip().lower()
            if not (mime.startswith("audio/") or content == "audio"):
                continue  # (video, text: not for a song)
            codec = (rep.get("codecs") or aset.get("codecs") or "").split(",")[0].strip()
            codec = _SAFE.sub("", codec)[:32]  # (it goes into reasons and the log)
            kind = _kind(codec) if mime == "audio/mp4" else None
            shown = codec or _SAFE.sub("", mime)[:32] or "?"
            reps.append(_Rep(period, aset, rep, shown, kind, _bandwidth(rep)))
    if not reps:
        raise Unsupported("no audio")
    playable = [r for r in reps if r.kind is not None]
    if not playable:
        names = ", ".join(sorted({r.codec for r in reps}))
        raise Unsupported(f"no AAC, FLAC, ALAC or MP3 audio in MP4 ({names[:80]})")
    chosen = _choose(playable, *quality)
    assert chosen.kind is not None
    base = url
    for level in (root, period, chosen.aset, chosen.rep):
        found = _children(level, "BaseURL")
        if found and (found[0].text or "").strip():
            base = urljoin(base, (found[0].text or "").strip())
    init, media, layout, timeline, times = _parts(chosen, base, seconds)
    _check_parts([*media] if init is None else [init, *media], url)
    return Plan(
        codec=chosen.codec,
        kind=chosen.kind,
        bandwidth=chosen.bandwidth,
        init=init,
        media=tuple(media),
        seconds=seconds or timeline,
        layout=layout,
        offered=tuple(f"{r.codec} {round(r.bandwidth / 1000)}k" for r in reps),
        durations=times.durations if times is not None else None,
        timescale=times.timescale if times is not None else 1,
        earliest=times.earliest if times is not None else 0,
    )


@dataclass(frozen=True)
class _Times:
    """The media segments' durations, their timescale and the first one's start."""

    durations: tuple[int, ...]
    timescale: int
    earliest: int


def _timed(times: list[tuple[int, int]], timescale: int) -> _Times | None:
    """A timeline's durations - None when it has gaps or overlaps of more than a
    millisecond (an index of its durations would put the later segments at other times)."""
    jitter = max(1, timescale // 1000)
    for (start, length), (following, _) in itertools.pairwise(times):
        if abs(start + length - following) > jitter:
            return None
    return _Times(tuple(d for _, d in times), timescale, times[0][0])


def _even(
    count: int, duration: float, timescale: int, seconds: float | None, offset: float
) -> _Times | None:
    """Segments of one ``duration`` each (the last as long as what is left of ``seconds``),
    from ``offset`` (the presentation time offset); None without ``seconds`` (the last one's
    length is not told)."""
    each = round(duration)
    if count <= 0 or each <= 0 or not seconds:
        return None
    left = round(seconds * timescale) - each * (count - 1)
    last = left if 0 < left <= each else each
    return _Times((each,) * (count - 1) + (last,), timescale, round(offset))


def _choose(reps: list[_Rep], low: str, high: str) -> _Rep:
    """The highest-ranked representation from ``low`` to ``high`` (``QUALITIES``)."""

    def inside(r: _Rep) -> bool:
        if r.kind in LOSSLESS:
            return high == "lossless"
        if low == "lossless":
            return False
        kbps = r.bandwidth / 1000
        above = low == "any" or kbps >= int(low) * (1 - BOUND_TOLERANCE)
        return above and (high == "lossless" or kbps <= int(high) * (1 + BOUND_TOLERANCE))

    def rank(r: _Rep) -> tuple[bool, int, int]:
        kind = 2 if r.codec.lower() == "mp4a.40.2" else 1 if r.kind == "aac" else 0
        return r.kind in LOSSLESS, r.bandwidth, kind

    within = [r for r in reps if inside(r)]
    if not within:
        offered = ", ".join(f"{r.codec} {round(r.bandwidth / 1000)}k" for r in reps)
        raise OutOfRange(f"no quality in the chosen range ({low} to {high}; {offered[:80]})")
    return max(within, key=rank)


def _parts(
    chosen: _Rep, base: str, seconds: float | None
) -> tuple[Part | None, list[Part], str, float | None, _Times | None]:
    """The chosen representation's init and media parts, its layout, the timeline's
    length when it has one, and the segments' durations when the manifest tells them."""
    levels = (chosen.period, chosen.aset, chosen.rep)
    # The layout the representation inherits: the one nearest to it; its attributes and
    # children come from every level that has it, the nearer level's first.
    kind = "SegmentBase"
    for level in levels:
        for layout in _LAYOUTS:
            if _children(level, layout):
                kind = layout
    found = [e for level in levels for e in _children(level, kind)[:1]]
    attrs: dict[str, str] = {}
    for element in found:
        attrs.update({k: v.strip() for k, v in element.attrib.items()})

    def child(name: str) -> list[Element]:
        for element in reversed(found):
            if items := _children(element, name):
                return items
        return []

    timescale = _number(attrs.get("timescale"), 1, high=2.0**32) or 1
    scale = int(timescale)
    offset = _number(attrs.get("presentationTimeOffset"), 0)
    rep_id = (chosen.rep.get("id") or "").strip()
    values = {"RepresentationID": rep_id, "Bandwidth": str(chosen.bandwidth)}
    if kind == "SegmentTemplate":
        start = int(_number(attrs.get("startNumber"), 1))
        init = attrs.get("initialization")
        init_part = Part(urljoin(base, _expand(init, values, start, 0))) if init else None
        media = attrs.get("media")
        if not media:
            raise Unsupported("a segment template without its media")
        timelines = child("SegmentTimeline")
        if timelines:
            times = _timeline(timelines[0])
            parts = [
                Part(urljoin(base, _expand(media, values, start + n, t)))
                for n, (t, _) in enumerate(times)
            ]
            total = sum(d for _, d in times) / timescale
            return init_part, parts, "SegmentTemplate+SegmentTimeline", total, _timed(times, scale)
        duration = _number(attrs.get("duration"), 0)
        if duration <= 0 or not seconds:
            raise Unsupported("a segment template whose segments cannot be counted")
        ratio = seconds * timescale / duration
        if ratio > MAX_SEGMENTS:
            raise Unsupported(f"more than {MAX_SEGMENTS:,} segments")
        count = math.ceil(ratio - 1e-6)
        parts = [
            Part(urljoin(base, _expand(media, values, start + n, round(n * duration))))
            for n in range(count)
        ]
        even = _even(count, duration, scale, seconds, offset)
        return init_part, parts, "SegmentTemplate@duration", None, even
    if kind == "SegmentList":
        inits = child("Initialization")
        init_part = _located(inits[0], "sourceURL", "range", base) if inits else None
        urls = child("SegmentURL")
        if not urls:
            raise Unsupported("a segment list without segments")
        if len(urls) > MAX_SEGMENTS:
            raise Unsupported(f"more than {MAX_SEGMENTS:,} segments")
        parts = [_located(u, "media", "mediaRange", base) for u in urls]
        told: _Times | None = None
        listed = child("SegmentTimeline")
        if listed:
            entries = _timeline(listed[0])
            if len(entries) == len(parts):
                told = _timed(entries, scale)
        elif (duration := _number(attrs.get("duration"), 0)) > 0:
            told = _even(len(parts), duration, scale, seconds, offset)
        return init_part, parts, "SegmentList", None, told
    # SegmentBase, or no layout at all: the representation is one file. A separate
    # initialization file is fetched first.
    inits = child("Initialization")
    source = inits[0].get("sourceURL") if inits else None
    init_part = Part(urljoin(base, source.strip())) if source and source.strip() else None
    return init_part, [Part(base)], "SegmentBase", None, None


def _check_parts(parts: list[Part], url: str) -> None:
    """Every part is an http(s) address, and none is the manifest itself (a representation
    that names no file of its own)."""
    for part in parts:
        _check_address(part.url)
        if part.url == url:
            raise Unsupported("a representation without an address of its own")


def _located(element: Element, address: str, span: str, base: str) -> Part:
    """A SegmentList's part: its address (the BaseURL's file when it names none), and the
    byte range it is of that file."""
    url = urljoin(base, (element.get(address) or "").strip())
    value = (element.get(span) or "").strip()
    if not value:
        return Part(url)
    found = _RANGE.fullmatch(value)
    if found is None or int(found[1]) > int(found[2]):
        raise Unsupported("a byte range that cannot be read")
    return Part(url, int(found[1]), int(found[2]))


def _timeline(timeline: Element) -> list[tuple[int, int]]:
    """A SegmentTimeline's segments: (start, duration) in its timescale."""
    out: list[tuple[int, int]] = []
    at = 0
    for s in _children(timeline, "S"):
        if s.get("t") is not None:
            at = int(_number(s.get("t"), 0))
        length = int(_number(s.get("d"), 0))
        try:
            repeat = int((s.get("r") or "0").strip())  # (-1: until the period's end)
        except ValueError:
            raise Unsupported("a number in the manifest that cannot be read") from None
        if length <= 0:
            raise Unsupported("a timeline segment without a length")
        if repeat < 0:
            raise Unsupported("an open-ended timeline")
        if len(out) + repeat + 1 > MAX_SEGMENTS:
            raise Unsupported(f"more than {MAX_SEGMENTS:,} segments")
        for _ in range(repeat + 1):
            out.append((at, length))
            at += length
    if not out:
        raise Unsupported("an empty timeline")
    return out


def _expand(template: str, values: dict[str, str], number: int, time: int) -> str:
    """A template with its tokens filled in ("$$" is a "$")."""
    filled = {**values, "Number": str(number), "Time": str(time)}

    def one(match: re.Match[str]) -> str:
        name, width = match[1], match[3]
        if name is None:
            if match[2]:
                raise Unsupported("a template token that cannot be read")
            return "$"
        if width and name == "RepresentationID":
            raise Unsupported("a template token that cannot be read")
        return filled[name].zfill(int(width)) if width else filled[name]

    parts = _TOKEN.split(template)
    if "$" in "".join(parts[:: 1 + _TOKEN.groups]):  # a "$" that starts no token it knows
        raise Unsupported("a template token Shijhon does not know")
    return _TOKEN.sub(one, template)


def _check_address(url: str) -> None:
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError):
        raise Unsupported("an address that cannot be read") from None
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise Unsupported("an address that is not http(s)")


def _kind(codec: str) -> str | None:
    lower = codec.lower()
    if lower in LOSSLESS:
        return lower
    if lower in _AAC:
        return "aac"
    if lower in _MP3:
        return "mp3"
    return None


def _seconds(value: str | None) -> float | None:
    """An ISO 8601 duration of days, hours, minutes and seconds ("PT5M5.667S")."""
    found = _DURATION.fullmatch((value or "").strip())
    if not value or found is None or not any(found.groups()):
        return None
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in found.groups())
    total = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    if not math.isfinite(total) or total > MAX_SECONDS:
        raise Unsupported("a duration that cannot be read")
    return total if total > 0 else None


def _number(value: str | None, default: float, high: float = 2.0**64) -> float:
    """A number of the manifest (its times and counts are 64-bit at most, its timescales
    32-bit: ``high``)."""
    if value is None or not value.strip():
        return default
    try:
        result = float(value)
    except ValueError:
        raise Unsupported("a number in the manifest that cannot be read") from None
    if not math.isfinite(result) or not 0 <= result <= high:
        raise Unsupported("a number in the manifest that cannot be read")
    return result


def _bandwidth(element: Element) -> int:
    """A representation's bits a second; 0 when it does not say (or cannot be read: a
    representation that is never chosen does not refuse the manifest)."""
    try:
        return int(_number(element.get("bandwidth"), 0))
    except Unsupported:
        return 0


def _name(element: Element) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(element: Element, name: str) -> list[Element]:
    return [child for child in element if _name(child) == name]


# --- the joined MP4 ----------------------------------------------------------------------

# Boxes that only encrypted audio has: an encrypted sample entry, its scheme, the key
# system's header, sample encryption.
_ENCRYPTED = {b"enca", b"encv", b"sinf", b"schm", b"tenc", b"pssh", b"senc"}
# Boxes that hold other boxes, down to the sample entries and the fragments' runs.
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"mvex", b"moof", b"traf", b"edts"}
_MAX_BOX_READ = 32 * 1024 * 1024  # a box read whole for its fields (not "mdat")
_MAX_DEPTH = 8  # containers within containers, at most


@dataclass
class Inspected:
    """What an MP4's boxes say: whether it is encrypted (or nests boxes too deep to tell),
    and its length in its own timescale - the fragments' sample durations added up, else
    the track's header's."""

    encrypted: bool = False
    timescale: int | None = None
    units: int = 0

    @property
    def seconds(self) -> float | None:
        return self.units / self.timescale if self.timescale and self.units else None


def inspect(source: BinaryIO | bytes, size: int | None = None) -> Inspected:
    """Read the boxes of an MP4 (a joined file, or an init segment's bytes); "mdat" is
    skipped, not read."""
    stream = io.BytesIO(source) if isinstance(source, bytes) else source
    if size is None:
        size = stream.seek(0, io.SEEK_END)
    found = Inspected()
    state = {"trex": 0, "fragments": 0, "header": 0}
    _walk(stream, 0, size, found, state)
    found.units = state["fragments"] or state["header"]
    return found


def _walk(
    stream: BinaryIO, start: int, end: int, found: Inspected, state: dict[str, int], depth: int = 0
) -> None:
    if depth > _MAX_DEPTH:  # (the boxes nest a few levels deep: deeper is no audio of ours)
        found.encrypted = True
        return
    default = state["trex"]
    for kind, payload, box_end in _boxes(stream, start, end):
        if kind in _ENCRYPTED:
            found.encrypted = True
        elif kind in _CONTAINERS:
            _walk(stream, payload, box_end, found, state, depth + 1)
        elif kind in (b"stsd", b"mdhd", b"trex", b"tfhd", b"trun"):
            data = _read(stream, payload, box_end)
            if kind == b"stsd":  # its sample entries' kinds (an encrypted one is "enca")
                entries = io.BytesIO(data)
                found.encrypted |= any(k in _ENCRYPTED for k, _, _ in _boxes(entries, 8, len(data)))
            elif kind == b"mdhd" and data:
                # Version 0: 32-bit times, then the timescale and the duration; 1: 64-bit.
                layout, at, unknown = (">II", 12, 0xFFFFFFFF) if data[0] == 0 else (">IQ", 20, -1)
                if len(data) >= at + struct.calcsize(layout):
                    timescale, length = struct.unpack_from(layout, data, at)
                    found.timescale = timescale or found.timescale
                    known = length != unknown and length != 0xFFFFFFFFFFFFFFFF
                    state["header"] += length if known else 0
            elif kind == b"trex" and len(data) >= 16:
                state["trex"] = default = struct.unpack_from(">I", data, 12)[0]
            elif kind == b"tfhd" and len(data) >= 8:
                flags = int.from_bytes(data[1:4], "big")
                at = 8 + (8 if flags & 0x1 else 0) + (4 if flags & 0x2 else 0)
                default = state["trex"]
                if flags & 0x8 and len(data) >= at + 4:
                    default = struct.unpack_from(">I", data, at)[0]
            elif kind == b"trun" and len(data) >= 8:
                state["fragments"] += _run_length(data, default)


def _run_length(data: bytes, default: int) -> int:
    """A track run's samples' durations added up (each sample's own, or the default)."""
    flags = int.from_bytes(data[1:4], "big")
    count = int(struct.unpack_from(">I", data, 4)[0])
    at = 8 + (4 if flags & 0x1 else 0) + (4 if flags & 0x4 else 0)
    if not flags & 0x100:
        return count * default
    step = 4 * bin(flags & 0xF00).count("1")
    if at + count * step > len(data):
        return 0
    return sum(int(struct.unpack_from(">I", data, at + n * step)[0]) for n in range(count))


def _boxes(stream: BinaryIO, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """The boxes between ``start`` and ``end``: (kind, where its payload starts, its end).
    A box that does not fit ends the walk."""
    at = start
    while at + 8 <= end:
        stream.seek(at)
        head = stream.read(16)
        if len(head) < 8:
            return
        size, kind = struct.unpack_from(">I4s", head)
        header = 8
        if size == 1:
            if len(head) < 16:
                return
            size, header = struct.unpack_from(">Q", head, 8)[0], 16
        elif size == 0:
            size = end - at
        if size < header or at + size > end:
            return
        yield kind, at + header, at + size
        at += size


def _read(stream: BinaryIO, start: int, end: int) -> bytes:
    if end - start > _MAX_BOX_READ:
        return b""
    stream.seek(start)
    return stream.read(end - start)


def flac_samples(head: bytes) -> tuple[int, int] | None:
    """A native FLAC's sample rate and total samples (its STREAMINFO, which must come first),
    from its first 26 bytes."""
    if len(head) < 26 or head[:4] != b"fLaC" or head[4] & 0x7F != 0:
        return None
    packed = int.from_bytes(head[18:26], "big")
    return packed >> 44, packed & ((1 << 36) - 1)


def with_samples(head: bytes, total: int) -> bytes:
    """``head`` (a native FLAC's first 26 bytes) with STREAMINFO's total samples set."""
    packed = int.from_bytes(head[18:26], "big")
    packed = (packed & ~((1 << 36) - 1)) | (total & ((1 << 36) - 1))
    return head[:18] + packed.to_bytes(8, "big")
