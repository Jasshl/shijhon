"""Local playlist documents: parse data without opening paths or following URLs."""

from __future__ import annotations

import configparser
import csv
import io
import json
import posixpath
import re
import unicodedata
from collections.abc import Iterable, Iterator
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from pydantic import Field, ValidationError, field_validator, model_validator

from shijhon.playlists.model import Model, PlaylistError, Suggestion, is_text

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 50_000
FORMATS = ("auto", "lines", "csv", "m3u", "pls", "xspf", "jspf", "asx", "wpl")
COLUMNS = {
    "artist": ("artist", "artists", "artistname", "artistnames", "creator"),
    "title": ("title", "track", "trackname", "song", "songname", "name"),
    "album": ("album", "albumname"),
    "version": ("version", "trackversion"),
    "location": ("location", "path", "filepath", "filename", "file", "uri", "trackuri", "url"),
    "native_id": ("nativeid", "navidromeid", "songid"),
}


class Entry(Model):
    """A caller's recording names and/or native identifiers and local path hints."""

    artist: str = Field(default="", max_length=300)
    title: str = Field(default="", max_length=300)
    album: str = Field(default="", max_length=300)
    version: str = Field(default="", max_length=100)
    locations: list[str] = Field(default_factory=list, max_length=16)
    native_id: str = Field(default="", max_length=256)

    @field_validator("artist", "title", "album", "version", "native_id")
    @classmethod
    def printable(cls, value: str) -> str:
        if not is_text(value):
            raise ValueError("entry fields must be printable")
        return value.strip()

    @field_validator("locations")
    @classmethod
    def local_text(cls, values: list[str]) -> list[str]:
        if any(len(v) > 4096 or not v.strip() or not is_text(v) for v in values):
            raise ValueError("invalid location")
        return [v.strip() for v in values]

    @model_validator(mode="after")
    def identifiable(self) -> Entry:
        if not (self.native_id or self.locations or (self.artist and self.title)):
            raise ValueError("each entry needs names, a location, or a native ID")
        if self.native_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", self.native_id):
            raise ValueError("invalid native ID")
        return self

    def suggestion(self) -> Suggestion | None:
        if not (self.artist and self.title):
            return None
        return Suggestion(
            artist=self.artist, title=self.title, album=self.album, version=self.version
        )

    def wanted(self) -> dict[str, Any]:
        suggestion = self.suggestion()
        if suggestion is None:
            raise PlaylistError("This entry needs a native path or recording choice.")
        return suggestion.wanted()


def _names(text: str) -> dict[str, str]:
    parts = re.split(r"\t| \u2014 | \u2013 | - ", text, maxsplit=1)
    return {"artist": parts[0], "title": parts[1]} if len(parts) == 2 else {"title": text}


def _header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _csv_rows(source: str) -> Iterator[list[str]]:
    # Trim only whole blank padding lines. str.strip() would remove real leading
    # and trailing TSV cells; filtering interior physical lines could damage quoted
    # multiline fields. A quoted boundary field includes its quote on either end.
    lines = source.splitlines(keepends=True)
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    source = "".join(lines[start:end])
    try:
        dialect = csv.Sniffer().sniff(source[:16384], delimiters=",;\t|")
    except csv.Error:
        # Quoted multiline metadata can confuse the dialect heuristic. A regular
        # header still identifies its separator; csv.reader handles the quoting.
        try:
            dialect = csv.Sniffer().sniff(source[:16384].splitlines()[0], delimiters=",;\t|")
        except (csv.Error, IndexError):
            dialect = csv.excel
    # Sniffer can miss doubled quote escaping around multiline metadata. Keep
    # standard CSV escaping even when a sample/header contains no visible escapes.
    return csv.reader(
        io.StringIO(source, newline=""), dialect=dialect, doublequote=True, strict=True
    )


def _csv(source: str, mapping: dict[str, str]) -> Iterable[dict[str, Any]]:
    reader = _csv_rows(source)
    headers = next(reader, [])
    normalized = [_header(h) for h in headers]
    if not headers:
        raise PlaylistError("Supply CSV column names and map the export columns on the form.")
    chosen: dict[str, int] = {}
    for field, aliases in COLUMNS.items():
        explicit = mapping.get(field, "").strip()
        matches = (
            [i for i, h in enumerate(headers) if h.strip() == explicit]
            if explicit
            else [i for i, h in enumerate(normalized) if h in aliases]
        )
        if explicit and not matches:
            raise PlaylistError(f"The mapped {field} column is absent. Check its exact header.")
        if len(matches) > 1:
            raise PlaylistError(f"Several columns can supply {field}. Map that column explicitly.")
        if matches:
            chosen[field] = matches[0]
    if not ("native_id" in chosen or "location" in chosen or {"artist", "title"} <= chosen.keys()):
        raise PlaylistError("Map artist and title, a file location, or a Navidrome song ID column.")
    for values in reader:
        if not values or not any(v.strip() for v in values):
            continue
        if len(values) != len(headers):
            raise ValueError("incomplete CSV row")
        row: dict[str, Any] = {k: values[i] for k, i in chosen.items() if k != "location"}
        if "location" in chosen and (location := values[chosen["location"]].strip()):
            row["locations"] = [location]
        yield row


def _m3u(source: str) -> Iterable[dict[str, Any]]:
    names: dict[str, str] = {}
    for line in source.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-"):
            raise PlaylistError("This is a streaming manifest. Upload a local music playlist.")
        if line.casefold().startswith("#extvlcopt:"):
            option = (
                line.split(":", 1)[1]
                .strip()
                .removeprefix(":")
                .strip()
                .split("=", 1)[0]
                .strip()
                .casefold()
            )
            if option in ("start-time", "stop-time", "run-time"):
                raise PlaylistError(
                    "Export timed playback instructions as an ordered whole-track list first."
                )
        if line.startswith("#EXTINF:"):
            if "," not in line:
                raise ValueError("invalid EXTINF")
            names = _names(line.split(",", 1)[1])
        elif not line.startswith("#"):
            yield {**names, "locations": [line]}
            names = {}


def _pls(source: str) -> Iterable[dict[str, Any]]:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(source)
    sections = [s for s in parser.sections() if s.casefold() == "playlist"]
    if len(sections) != 1:
        raise ValueError("missing playlist section")
    section = parser[sections[0]]
    indexes = sorted(int(k[4:]) for k in section if re.fullmatch(r"file[1-9][0-9]*", k))
    if indexes != list(range(1, len(indexes) + 1)):
        raise ValueError("missing PLS entry")
    if "numberofentries" in section and section.getint("numberofentries") != len(indexes):
        raise ValueError("PLS count mismatch")
    for i in indexes:
        yield {**_names(section.get(f"title{i}", "")), "locations": [section[f"file{i}"]]}


def _tag(element: Element) -> str:
    return element.tag.rsplit("}", 1)[-1].casefold()


def _attr(element: Element, name: str) -> str:
    return next((v for k, v in element.attrib.items() if k.casefold() == name), "")


def _uri(base: str, reference: str) -> str:
    """Resolve references lexically, including hierarchical media URI schemes."""
    base, reference = base.replace("\\", "/"), reference.replace("\\", "/")
    parsed = urlsplit(base)
    if parsed.scheme and not urlsplit(reference).scheme:
        # urljoin only inherits schemes on its built-in allowlist (not e.g. mms).
        # Use its URI path/authority algorithm without losing an unfamiliar scheme
        # and accidentally turning a remote reference into a native filename.
        joined = urlsplit(urljoin(urlunsplit(parsed._replace(scheme="http")), reference))
        return urlunsplit(joined._replace(scheme=parsed.scheme))
    return urljoin(base, reference)


def _bases(root: Element, *, asx: bool = False) -> dict[Element, str]:
    bases: dict[Element, str] = {}
    for parent in root.iter():
        base = _uri(
            bases.get(parent, ""), parent.get("{http://www.w3.org/XML/1998/namespace}base", "")
        )
        if asx:
            declared = [child for child in parent if _tag(child) == "base"]
            if len(declared) > 1:
                raise ValueError("ambiguous ASX base")
            if declared:
                value = _attr(declared[0], "href")
                if not value:
                    raise ValueError("missing ASX base")
                # ASX BASE names a directory, including when its slash is omitted.
                base = _uri(base, value.rstrip("/\\") + "/")
        bases[parent] = base
        for child in parent:
            bases[child] = base
    return bases


def _repeated(
    values: Iterable[dict[str, Any]], count: str, max_rows: int, *, zero_once: bool = False
) -> Iterable[dict[str, Any]]:
    try:
        copies = Decimal(count)
    except InvalidOperation:
        raise PlaylistError(
            "Export indefinite repeats as a finite ordered track list first."
        ) from None
    if not copies.is_finite():
        raise PlaylistError("Export indefinite repeats as a finite ordered track list first.")
    if copies == 0 and zero_once:
        copies = Decimal(1)  # ASX COUNT=0 means play the group once.
    if copies < 1 or copies != copies.to_integral_value():
        raise PlaylistError("Export indefinite or partial repeats as an ordered track list first.")
    group = list(islice(values, max_rows + 1))
    if not group:
        return
    if len(group) > max_rows or copies > max_rows // len(group):
        raise PlaylistError("Expanded repeats exceed the administrator's entry budget.")
    for _ in range(int(copies)):
        yield from group


def _asx(root: Element, max_rows: int, bases: dict[Element, str]) -> Iterable[dict[str, Any]]:
    for entry in root:
        if _tag(entry) == "entry":
            row: dict[str, Any] = {"locations": []}
            for child in entry:
                if _tag(child) == "ref" and _attr(child, "href"):
                    row["locations"].append(_uri(bases[child], _attr(child, "href")))
                elif _tag(child) == "title":
                    row.update(_names(child.text or ""))
            yield row
        elif _tag(entry) == "repeat":
            yield from _repeated(
                _asx(entry, max_rows, bases), _attr(entry, "count"), max_rows, zero_once=True
            )


def _wpl(root: Element, max_rows: int, bases: dict[Element, str]) -> Iterable[dict[str, Any]]:
    if any(
        k.casefold()
        in (
            "repeatdur",
            "clipbegin",
            "clipend",
            "clip-begin",
            "clip-end",
            "begin",
            "end",
            "dur",
            "min",
            "max",
            "endsync",
        )
        for k in root.attrib
    ):
        raise PlaylistError("Export timed playback instructions as an ordered track list first.")
    if _tag(root) in ("par", "switch", "excl", "priorityclass", "smartplaylist", "queryset"):
        raise PlaylistError(
            "Export dynamic, parallel or conditional playback as an ordered track list first."
        )
    values: Iterable[dict[str, Any]]
    if _tag(root) == "media":
        values = [{"locations": [_uri(bases[root], _attr(root, "src"))]}]
    else:
        values = (row for child in root for row in _wpl(child, max_rows, bases))
    if count := _attr(root, "repeatcount") or _attr(root, "repeat"):
        yield from _repeated(values, count, max_rows)
    else:
        yield from values


def _xml(source: str, format: str, max_rows: int) -> Iterable[dict[str, Any]]:
    root = fromstring(source, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    if _tag(root) != {"xspf": "playlist", "asx": "asx", "wpl": "smil"}[format]:
        raise ValueError("wrong playlist document")
    bases = _bases(root, asx=format == "asx")
    if format == "xspf":
        namespace = "{http://xspf.org/ns/0/}"
        tracks = root.find(namespace + "trackList")
        if tracks is None:
            tracks = root.find("trackList")
        if tracks is None:
            raise ValueError("missing track list")
        for track in tracks:
            if _tag(track) != "track":
                continue
            row: dict[str, Any] = {"locations": []}
            for child in track:
                tag, text = _tag(child), (child.text or "").strip()
                if tag in ("title", "album", "creator"):
                    row["artist" if tag == "creator" else tag] = text
                elif tag == "location" and text:
                    row["locations"].append(_uri(bases[child], text))
                elif tag == "identifier" and text.startswith(("navidrome:", "subsonic:")):
                    row["native_id"] = text.split(":", 1)[1]
            yield row
    elif format == "asx":
        for child in root.iter():
            if _tag(child) in (
                "starttime",
                "duration",
                "previewduration",
                "startmarker",
                "endmarker",
            ):
                raise PlaylistError(
                    "Export timed playback instructions as an ordered track list first."
                )
            if _tag(child) == "event":
                raise PlaylistError("Export conditional playback as an ordered track list first.")
            if _tag(child) == "entryref":
                raise PlaylistError(
                    "Referenced playlists are not opened. Export their ordered tracks first."
                )
        yield from _asx(root, max_rows, bases)
    else:
        yield from _wpl(root, max_rows, bases)


def _jspf(source: str) -> Iterable[dict[str, Any]]:
    playlist = json.loads(source)["playlist"]
    for track in playlist["track"]:
        locations = track.get("location", [])
        if isinstance(locations, str):
            locations = [locations]
        yield {
            "artist": track.get("creator", ""),
            "title": track.get("title", ""),
            "album": track.get("album", ""),
            "locations": locations,
        }


def detect(source: str, filename: str = "") -> str:
    table_source = source.lstrip("\ufeff")
    source = table_source.strip()
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.casefold()
    if suffix in (".m3u", ".m3u8") or source.startswith("#EXTM3U"):
        return "m3u"
    if suffix == ".pls" or source[:10].casefold().startswith("[playlist]"):
        return "pls"
    if suffix in (".xspf", ".asx", ".wpl", ".jspf"):
        return suffix[1:]
    if source.startswith("<"):
        # Sniff XML without an unsafe preliminary parser.
        root = fromstring(source, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        return {"playlist": "xspf", "asx": "asx", "smil": "wpl"}.get(_tag(root), "unknown")
    if suffix == ".json" or source.startswith("{"):
        return "jspf"
    if suffix in (".csv", ".tsv"):
        return "csv"
    first = source.splitlines()[0] if source else ""
    try:
        headers = [_header(v) for v in next(_csv_rows(table_source), [])]
    except csv.Error:
        headers = []  # A quote in an artist/title line does not make it malformed CSV.
    fields = {field for field, aliases in COLUMNS.items() if any(h in aliases for h in headers)}
    names = _names(first)
    if re.match(r"^(?:[A-Za-z]:[\\/]|[/\\]|\.\.?[\\/]|[A-Za-z][A-Za-z0-9+.-]*://)", first):
        return "m3u"
    if "artist" in names and "\t" not in names["title"]:
        # Two tab-separated values also resemble a table header. Require a complete
        # recording header or a leading named field identifying native ID/path data;
        # a title like "Name" or "Path" alone is not evidence of an export header.
        if {"artist", "title"} <= fields or (
            fields & {"location", "native_id"}
            and headers
            and headers[0] in sum(COLUMNS.values(), ())
        ):
            return "csv"
        return "lines"
    if fields:
        return "csv"
    if first and ("/" in first or "\\" in first or re.search(r"\.[A-Za-z0-9]{2,5}$", first)):
        return "m3u"
    return "lines"


def parse(
    source: str,
    *,
    csv_file: bool = False,
    format: str = "auto",
    filename: str = "",
    mapping: dict[str, str] | None = None,
    max_bytes: int = MAX_BYTES,
    max_rows: int = MAX_ROWS,
) -> list[Entry]:
    """Malformed entries reject the entire document; never silently lose music rows."""
    if len(source.encode("utf-8")) > max_bytes:
        raise PlaylistError(f"This import exceeds the administrator's {max_bytes:,}-byte budget.")
    table_source = source.lstrip("\ufeff")
    source = table_source.strip()
    rows: list[Entry] = []
    try:
        selected = (
            "csv" if csv_file else detect(table_source, filename) if format == "auto" else format
        )
        if selected not in FORMATS:
            raise PlaylistError("Choose a supported local playlist format.")
        values: Iterable[dict[str, Any]]
        if selected == "csv":
            values = _csv(table_source, mapping or {})
        elif selected == "m3u":
            values = _m3u(source)
        elif selected == "pls":
            values = _pls(source)
        elif selected in ("xspf", "asx", "wpl"):
            values = _xml(source, selected, max_rows)
        elif selected == "jspf":
            values = _jspf(source)
        else:
            values = (_names(line.strip()) for line in source.splitlines() if line.strip())
        for value in values:
            if len(rows) >= max_rows:
                raise PlaylistError(
                    f"This import exceeds the administrator's {max_rows:,}-entry budget."
                )
            rows.append(Entry.model_validate(value))
    except (
        ValidationError,
        csv.Error,
        configparser.Error,
        ParseError,
        DefusedXmlException,
        TypeError,
        ValueError,
        KeyError,
        RecursionError,
        AttributeError,
    ):
        raise PlaylistError(
            f"Import entry {len(rows) + 1} or its document is invalid. Check the format and column "
            "mapping. Each entry needs artist/title, a file location, or a native song ID. "
            "No entries were imported."
        ) from None
    if not rows:
        raise PlaylistError("Supply at least one playlist entry.")
    return rows


def local_path(location: str) -> str:
    """Lexical path hint only; never resolve, stat or open a filesystem path."""
    value = location.strip().replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", value):
        value = value[2:]
    else:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        if parsed.scheme:
            if parsed.scheme.casefold() != "file" or parsed.netloc not in ("", "localhost"):
                return ""
            value = unquote(parsed.path)
        elif value.startswith("//"):
            # UNC locations are matched by their lexical path, never accessed.
            value = value.lstrip("/")
    normalized = posixpath.normpath(value).strip("/")
    while normalized.startswith("../"):
        normalized = normalized[3:]
    return unicodedata.normalize("NFC", normalized) if normalized not in ("", ".", "..") else ""
