"""The XML form of Subsonic answers, written as Navidrome 0.64.2 writes it.

Every answer Shijhon produces or extends has one model: the JSON-shaped document of
Navidrome's JSON answer (``subsonic-response`` and its payload). Its XML form is written
from that same document with Navidrome's response types (``server/subsonic/responses/
responses.go``): which keys are attributes and which are child elements, their order
(attributes, then elements, each in the types' field order), which values are left out when
empty (XML's ``omitempty``, which the JSON form of the OpenSubsonic fields does not have),
and Go's escaping. No XML declaration; the namespace on the root element only.

Navidrome's own XML answer, when Shijhon adds to it, is read into the same JSON-shaped
document (with the keys JSON always carries, so the additions are computed from the same
data as for JSON), and only what Shijhon changed is written back: Navidrome's own entries
keep their elements - attributes, order, children, also any this module does not know - so
an answer Shijhon does not change comes out byte for byte as it came. Only the types of the
answers Shijhon produces or extends are described here; an element of another type in
Navidrome's answer is kept as it came.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from xml.etree.ElementTree import Element, XMLParser

NAMESPACE = "http://subsonic.org/restapi"
ROOT = "subsonic-response"
CONTENT_TYPE = b"application/xml"  # Navidrome's, without a charset
_Q = "{" + NAMESPACE + "}"

ATTR, LIST, TEXTS, STRUCT, TEXT = "attr", "list", "texts", "struct", "text"
_SCALARS = {"s": str, "i": int, "b": bool, "f": float}


@dataclass(frozen=True)
class Field:
    """One key of a type: its name (the same in JSON and XML for these types), its kind
    (an attribute, a list of elements of a type, a list of text elements, one element of a
    type, one text element), its type, whether XML writes it when empty and whether JSON
    always carries it."""

    key: str
    kind: str
    type: str
    xml_always: bool = False
    json_always: bool = False


def _fields(spec: str) -> tuple[Field, ...]:
    """``name:type`` pairs in field order; ``!``: XML writes it even when empty; ``*``: JSON
    always carries it. Types: ``s``/``i``/``b``/``f`` (an attribute), ``[s]`` (text
    elements), ``[Type]`` (elements of a type), ``Type`` (one element), ``text`` (one text
    element)."""
    out = []
    for token in spec.split():
        name, _, kind = token.partition(":")
        flags = kind.lstrip("[").rstrip("]!*")
        always, json_always = "!" in kind, "*" in kind
        listed = kind.startswith("[")
        if listed:
            out.append(Field(name, TEXTS if flags == "s" else LIST, flags, always, json_always))
        elif flags in _SCALARS:
            out.append(Field(name, ATTR, flags, always, json_always))
        elif flags == "text":
            out.append(Field(name, TEXT, "s", always, json_always))
        else:
            out.append(Field(name, STRUCT, flags, always, json_always))
    return tuple(out)


# Navidrome 0.64.2's response types (embedded OpenSubsonic types flattened in place).
TYPES: dict[str, tuple[Field, ...]] = {
    "Subsonic": _fields(
        "status:s!* version:s!* type:s!* serverVersion:s!* openSubsonic:b"
        " error:Error directory:Directory albumList2:AlbumList2 searchResult3:SearchResult3"
        " starred2:Starred2 song:Child randomSongs:Songs artist:ArtistWithAlbumsID3"
        " album:AlbumWithSongsID3 albumInfo:AlbumInfo artistInfo:ArtistInfo"
        " artistInfo2:ArtistInfo2 similarSongs:Songs similarSongs2:Songs topSongs:Songs"
        " lyricsList:LyricsList"
    ),
    "Error": _fields("code:i!* message:s!*"),
    "Child": _fields(
        "id:s!* parent:s isDir:b!* title:s!* name:s album:s artist:s track:i year:i genre:s"
        " coverArt:s size:i contentType:s suffix:s starred:s transcodedContentType:s"
        " transcodedSuffix:s duration:i bitRate:i path:s playCount:i discNumber:i created:s"
        " albumId:s artistId:s type:s userRating:i averageRating:f songCount:i isVideo:b"
        " bookmarkPosition:i"
        # OpenSubsonicChild
        " played:s bpm:i* comment:s* sortName:s* mediaType:s* musicBrainzId:s* isrc:[s]*"
        " genres:[ItemGenre]* replayGain:ReplayGain* channelCount:i* samplingRate:i*"
        " bitDepth:i* moods:[s]* artists:[ArtistID3Ref]* displayArtist:s*"
        " albumArtists:[ArtistID3Ref]* displayAlbumArtist:s* contributors:[Contributor]*"
        " displayComposer:s* explicitStatus:s* groupings:[s]* works:[Work]*"
        " movements:[Movement]*"
    ),
    "Songs": _fields("song:[Child]"),
    "LyricsList": (),  # written empty only; lyrics Navidrome writes are kept as they came
    "Directory": _fields(
        "child:[Child] id:s!* name:s!* parent:s starred:s playCount:i played:s userRating:i"
        " averageRating:f artist:s artistId:s coverArt:s songCount:i albumCount:i duration:i"
        " created:s year:i genre:s"
    ),
    "ArtistID3Ref": _fields("id:s!* name:s!*"),
    "Artist": _fields(
        "id:s!* name:s!* starred:s userRating:i averageRating:f coverArt:s artistImageUrl:s"
    ),
    "ArtistID3": _fields(
        "id:s!* name:s!* coverArt:s albumCount:i!* starred:s userRating:i averageRating:f"
        " artistImageUrl:s musicBrainzId:s* sortName:s* roles:[s]*"
    ),
    "AlbumID3": _fields(
        "id:s!* name:s!* artist:s artistId:s coverArt:s songCount:i!* duration:i!* playCount:i"
        " created:s!* starred:s year:i genre:s"
        # OpenSubsonicAlbumID3
        " played:s userRating:i* averageRating:f genres:[ItemGenre]* musicBrainzId:s*"
        " isCompilation:b* sortName:s* discTitles:[DiscTitle]*"
        " originalReleaseDate:ItemDate* releaseDate:ItemDate* releaseTypes:[s]*"
        " recordLabels:[RecordLabel]* moods:[s]* artists:[ArtistID3Ref]* displayArtist:s*"
        " explicitStatus:s* version:s*"
    ),
    "AlbumList2": _fields("album:[AlbumID3]"),
    "SearchResult3": _fields("artist:[ArtistID3] album:[AlbumID3] song:[Child]"),
    "Starred2": _fields("artist:[ArtistID3] album:[AlbumID3] song:[Child]"),
    "AlbumInfo": _fields(
        "notes:text musicBrainzId:text lastFmUrl:text smallImageUrl:text mediumImageUrl:text"
        " largeImageUrl:text"
    ),
    "ArtistInfo": _fields(
        "biography:text musicBrainzId:text lastFmUrl:text smallImageUrl:text"
        " mediumImageUrl:text largeImageUrl:text similarArtist:[Artist]"
    ),
    "ArtistInfo2": _fields(
        "biography:text musicBrainzId:text lastFmUrl:text smallImageUrl:text"
        " mediumImageUrl:text largeImageUrl:text similarArtist:[ArtistID3]"
    ),
    "ItemGenre": _fields("name:s!*"),
    "RecordLabel": _fields("name:s!*"),
    "Work": _fields("name:s!* musicBrainzId:s"),
    "Movement": _fields("name:s!* number:i count:i"),
    "DiscTitle": _fields("disc:i!* title:s!* coverArt:s"),
    "ItemDate": _fields("year:i month:i day:i"),
    # Pointers in Navidrome: a value present is written, zero too.
    "ReplayGain": _fields(
        "trackGain:f! albumGain:f! trackPeak:f! albumPeak:f! baseGain:f! fallbackGain:f!"
    ),
    "Contributor": _fields("role:s!* subRole:s artist:ArtistID3Ref*"),
}
# ArtistWithAlbumsID3 and AlbumWithSongsID3 embed ArtistID3 and AlbumID3.
TYPES["ArtistWithAlbumsID3"] = (*TYPES["ArtistID3"], *_fields("album:[AlbumID3]"))
TYPES["AlbumWithSongsID3"] = (*TYPES["AlbumID3"], *_fields("song:[Child]"))

_MISSING: Any = object()


def _omitted(type_name: str, value: dict[str, Any]) -> bool:
    """Types that write no element at all when empty (their own MarshalXML): a date whose
    parts are all zero, replay gain without any value."""
    if type_name == "ItemDate":
        return not any(value.values())
    if type_name == "ReplayGain":
        return all(v is None for v in value.values())
    return False


# --- writing ---------------------------------------------------------------------------------

_ESCAPES = {
    ord('"'): "&#34;",
    ord("'"): "&#39;",
    ord("&"): "&amp;",
    ord("<"): "&lt;",
    ord(">"): "&gt;",
    ord("\t"): "&#x9;",
    ord("\n"): "&#xA;",
    ord("\r"): "&#xD;",
}
# Characters XML cannot carry: Go writes U+FFFD for them.
_INVALID = re.compile(
    f"[^\t\n\r\x20-{chr(0xD7FF)}{chr(0xE000)}-{chr(0xFFFD)}{chr(0x10000)}-{chr(0x10FFFF)}]"
)


def escape(text: str) -> str:
    """Text or an attribute value escaped as Go's encoding/xml escapes it."""
    text = text.translate(_ESCAPES)
    return _INVALID.sub("\N{REPLACEMENT CHARACTER}", text) if _INVALID.search(text) else text


def go_float(value: float) -> str:
    """A float as Go writes one in XML (``strconv.FormatFloat(v, 'g', -1, 64)``)."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    _, digits_tuple, exponent = Decimal(repr(abs(value))).normalize().as_tuple()
    assert isinstance(exponent, int)
    digits = "".join(map(str, digits_tuple))
    point = len(digits) + exponent  # where the decimal point goes
    exp = point - 1
    minus = "-" if value < 0 else ""
    if exp < -4 or exp >= 6:  # %e (the shortest precision decides with 6)
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{minus}{mantissa}e{'-' if exp < 0 else '+'}{abs(exp):02d}"
    if point <= 0:
        return f"{minus}0.{'0' * -point}{digits}"
    if point >= len(digits):
        return minus + digits + "0" * (point - len(digits))
    return f"{minus}{digits[:point]}.{digits[point:]}"


def _attribute(value: Any, field: Field) -> str | None:
    """The attribute's text, or None when it is left out."""
    if value is None or value is _MISSING or isinstance(value, list | dict):
        return None
    if not value and not field.xml_always:
        return None  # omitempty
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return go_float(value)
    return str(value)


def _text_element(name: str, text: str) -> Element:
    element = Element(_Q + name)
    element.text = text
    return element


def _elements(value: Any, field: Field) -> list[Element]:
    """The elements a list, struct or text field writes."""
    if field.kind == LIST:
        items = value if isinstance(value, list) else []
        return [build(item, field.type, field.key) for item in items if isinstance(item, dict)]
    if field.kind == TEXTS:
        items = value if isinstance(value, list) else []
        return [_text_element(field.key, str(item)) for item in items if item is not None]
    if field.kind == STRUCT:
        if not isinstance(value, dict):
            return []
        if _omitted(field.type, value):
            return []
        return [build(value, field.type, field.key)]
    if value is None or value is _MISSING or value == "":
        return []  # a text element: omitempty
    return [_text_element(field.key, str(value))]


def build(value: dict[str, Any], type_name: str, name: str) -> Element:
    """A JSON-shaped entry of a type as its XML element; keys the type does not have are
    left out (Navidrome has no such keys either)."""
    element = Element(_Q + name)
    fields = TYPES[type_name]
    for field in fields:
        if field.kind == ATTR:
            text = _attribute(value.get(field.key, _MISSING), field)
            if text is not None:
                element.set(field.key, text)
    for field in fields:
        if field.kind != ATTR and field.key in value:
            element.extend(_elements(value[field.key], field))
    return element


def unknown(value: dict[str, Any], type_name: str, path: str = "") -> list[str]:
    """Keys of a JSON-shaped entry (and of the entries in it) that its type does not have,
    which its XML form would leave out."""
    fields = {f.key: f for f in TYPES[type_name]}
    found = []
    for key, item in value.items():
        field = fields.get(key)
        if field is None:
            found.append(f"{path}{key}")
        elif field.kind == LIST and isinstance(item, list):
            for n, entry in enumerate(item):
                if isinstance(entry, dict):
                    found += unknown(entry, field.type, f"{path}{key}[{n}].")
        elif field.kind == STRUCT and isinstance(item, dict):
            found += unknown(item, field.type, f"{path}{key}.")
    return found


def serialize(root: Element) -> bytes:
    """The document as Navidrome writes it: no declaration, the namespace on the root, every
    element with an end tag, Go's escaping."""
    out: list[str] = []
    _write(root, out, top=True)
    return "".join(out).encode()


def _write(element: Element, out: list[str], *, top: bool = False) -> None:
    tag = _local(element.tag)
    out.append("<" + tag)
    if top:
        out.append(f' xmlns="{NAMESPACE}"')
    for key, value in element.attrib.items():
        out.append(f' {_local(key)}="{escape(value)}"')
    out.append(">")
    if element.text:
        out.append(escape(element.text))
    for child in element:
        _write(child, out)
    out.append(f"</{tag}>")
    if element.tail and not top:
        out.append(escape(element.tail))


def answer(fields: dict[str, Any], payload: dict[str, Any]) -> bytes:
    """Shijhon's own answer: the envelope's ``fields`` (status, version, type, ...) and the
    payload (e.g. ``{"album": {...}}``), written as Navidrome writes the same answer."""
    return serialize(build({**fields, **payload}, "Subsonic", ROOT))


# --- reading Navidrome's answer -------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _value(text: str, type_code: str) -> Any:
    if type_code == "b":
        return text == "true"
    try:
        if type_code == "i":
            return int(text)
        if type_code == "f":
            return float(text)
    except ValueError:
        return text  # as it came: Navidrome's own value is never wrong here
    return text


def _zero(field: Field) -> Any:
    if field.kind in (LIST, TEXTS):
        return []
    if field.kind == STRUCT:
        return {}
    return _SCALARS.get(field.type, str)()


@dataclass
class _Origin:
    """An entry read from Navidrome's element, and its values as read (to tell changes)."""

    value: dict[str, Any]
    element: Element
    type_name: str
    before: dict[str, Any]


class XmlDocument(dict[str, Any]):
    """Navidrome's XML answer read into its JSON shape (``{"subsonic-response": ...}``);
    ``dumps`` writes it back with Shijhon's changes."""

    def __init__(self, root: Element, response: dict[str, Any], origins: dict[int, _Origin]):
        super().__init__({"subsonic-response": response})
        self.root = root
        self.origins = origins


def load(body: bytes) -> XmlDocument:
    """Navidrome's XML answer as a JSON-shaped document; ValueError when it is not one."""
    # Navidrome's own answer; expat resolves no external entities and limits entity
    # expansion (billion laughs) itself.
    parser = XMLParser()  # noqa: S314
    try:
        parser.feed(body)
        root = parser.close()
    except Exception as exc:  # expat's ParseError, or anything a broken answer raises
        raise ValueError(f"not XML: {type(exc).__name__}") from None
    if root.tag != _Q + ROOT:
        raise ValueError("not a Subsonic answer")
    origins: dict[int, _Origin] = {}
    response = _read(root, "Subsonic", origins)
    return XmlDocument(root, response, origins)


def _read(element: Element, type_name: str, origins: dict[int, _Origin]) -> dict[str, Any]:
    fields = TYPES[type_name]
    value: dict[str, Any] = {}
    attributes = element.attrib
    groups: dict[str, list[Element]] = {}
    for child in element:
        groups.setdefault(_local(child.tag), []).append(child)
    for field in fields:
        if field.kind == ATTR:
            raw = attributes.get(field.key)
            if raw is not None:
                value[field.key] = _value(raw, field.type)
            elif field.json_always:
                value[field.key] = _zero(field)
            continue
        found = groups.get(field.key, [])
        if field.kind == LIST:
            read: Any = [_read(child, field.type, origins) for child in found]
        elif field.kind == TEXTS:
            read = [child.text or "" for child in found]
        elif field.kind == STRUCT:
            read = _read(found[0], field.type, origins) if found else _MISSING
        else:
            read = (found[0].text or "") if found else _MISSING
        if read is _MISSING or (field.kind in (LIST, TEXTS) and not read):
            if field.json_always:  # as JSON carries it: empty
                value[field.key] = _zero(field)
            continue
        value[field.key] = read
    origins[id(value)] = _Origin(value, element, type_name, _snapshot(value, fields))
    return value


def _snapshot(value: dict[str, Any], fields: tuple[Field, ...]) -> dict[str, Any]:
    """What each field held when read: scalars and texts by value, entries themselves (kept
    here, compared by identity)."""
    before: dict[str, Any] = {}
    for field in fields:
        if field.key in value:
            now = value[field.key]
            if field.kind in (LIST, TEXTS):
                before[field.key] = tuple(now)
            elif field.kind == STRUCT:  # itself, and what it held (one made empty is untracked)
                before[field.key] = (now, dict(now))
            else:
                before[field.key] = now
    return before


def _same_entries(now: Any, before: Any) -> bool:
    """The same entries (the same objects) in the same order."""
    return (
        isinstance(now, list)
        and isinstance(before, tuple)
        and len(now) == len(before)
        and all(a is b for a, b in zip(now, before, strict=True))
    )


def dumps(document: dict[str, Any]) -> bytes:
    """The document written back: Navidrome's elements as they came, with what changed (a
    document not read from XML: written whole)."""
    response = document.get("subsonic-response")
    if not isinstance(response, dict):
        raise ValueError("no subsonic-response")
    origins = document.origins if isinstance(document, XmlDocument) else {}
    origin = origins.get(id(response))
    if origin is None or origin.value is not response:
        return serialize(build(response, "Subsonic", ROOT))
    _sync(response, origin, origins)
    assert isinstance(document, XmlDocument)
    return serialize(document.root)


def _same(now: Any, before: Any) -> bool:
    return type(now) is type(before) and now == before


def _sync(value: dict[str, Any], origin: _Origin, origins: dict[int, _Origin]) -> None:
    """Bring the entry's element up to date with the entry (in place)."""
    element, before = origin.element, origin.before
    fields = TYPES[origin.type_name]
    added = False
    for field in fields:
        if field.kind != ATTR:
            continue
        now = value.get(field.key, _MISSING)
        if _same(now, before.get(field.key, _MISSING)):
            continue
        text = _attribute(now, field)
        if text is None:
            element.attrib.pop(field.key, None)
        else:
            added = added or field.key not in element.attrib
            element.set(field.key, text)
    if added:
        _order_attributes(element, fields)
    for field in fields:
        if field.kind != ATTR:
            _sync_field(value, field, element, fields, before, origins)


def _sync_field(
    value: dict[str, Any],
    field: Field,
    element: Element,
    fields: tuple[Field, ...],
    before: dict[str, Any],
    origins: dict[int, _Origin],
) -> None:
    now = value.get(field.key, _MISSING)
    was = before.get(field.key, _MISSING)
    if field.kind == LIST:
        items = [i for i in now if isinstance(i, dict)] if isinstance(now, list) else []
        mine = {id(c) for c in element if _local(c.tag) == field.key}
        if _same_entries(now, was):
            for item in items:  # the same entries: each brought up to date in place
                if (found := _tracked(item, origins, mine)) is not None:
                    _sync(item, found, origins)
            return
        new = []
        for item in items:
            found = _tracked(item, origins, mine)
            if found is not None:
                _sync(item, found, origins)
                new.append(found.element)
            else:
                new.append(build(item, field.type, field.key))
        _replace(element, fields, field, new)
    elif field.kind == STRUCT:
        was_value, was_items = was if was is not _MISSING else (_MISSING, None)
        if isinstance(now, dict) and now is was_value:
            mine = {id(c) for c in element if _local(c.tag) == field.key}
            if (found := _tracked(now, origins, mine)) is not None:
                _sync(now, found, origins)
                return
            if now == was_items:
                return  # the empty one JSON carries, as it was
        elif now is _MISSING and was is _MISSING:
            return
        _replace(element, fields, field, _elements(now, field))
    elif field.kind == TEXTS:
        if isinstance(now, list) and was == tuple(now):
            return
        if now is _MISSING and was is _MISSING:
            return
        _replace(element, fields, field, _elements(now, field))
    else:
        if _same(now, was):
            return
        _replace(element, fields, field, _elements(now, field))


def _tracked(item: dict[str, Any], origins: dict[int, _Origin], mine: set[int]) -> _Origin | None:
    """The origin of an entry read from one of this element's own children."""
    found = origins.get(id(item))
    if found is None or found.value is not item or id(found.element) not in mine:
        return None
    return found


def _replace(element: Element, fields: tuple[Field, ...], field: Field, new: list[Element]) -> None:
    """Put ``new`` where the field's elements are (in the types' order when there were
    none), in place of those."""
    children = list(element)
    positions = [i for i, c in enumerate(children) if _local(c.tag) == field.key]
    if positions:
        at = positions[0]
    else:
        order = {f.key: n for n, f in enumerate(fields) if f.kind != ATTR}
        rank = order[field.key]
        at = 0
        for index, child in enumerate(children):
            other = order.get(_local(child.tag))
            if other is not None and other < rank:
                at = index + 1
    for index in reversed(positions):
        del element[index]
    for offset, child in enumerate(new):
        element.insert(at + offset, child)


def _order_attributes(element: Element, fields: tuple[Field, ...]) -> None:
    """Attributes in the types' order after one was added (unknown ones after them)."""
    order = {f.key: n for n, f in enumerate(fields) if f.kind == ATTR}
    items = list(element.attrib.items())
    known = sorted((kv for kv in items if kv[0] in order), key=lambda kv: order[kv[0]])
    unknown = [kv for kv in items if kv[0] not in order]
    element.attrib.clear()
    element.attrib.update(known + unknown)
