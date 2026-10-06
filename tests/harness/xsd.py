"""The Subsonic XML schema (``subsonic-rest-api.xsd``, API 1.16.1) with the OpenSubsonic
additions Navidrome 0.64.2 emits, for validating XML answers (xmlschema).

The schema is downloaded once from subsonic.org, checked against its SHA-256 and cached
(like the Navidrome binary); it is not part of this repository. The additions are written
here by hand from Navidrome's response types and the OpenSubsonic specification -
independently of Shijhon's own ``proxy/xmlform.py`` - and Navidrome's own answers are
validated with them too, so they describe what Navidrome emits. XSD sequences fix the order
of child elements, so the order is checked as well. ``same_data`` compares an XML answer with
the JSON answer, entry by entry.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from functools import cache
from typing import Any

import httpx
import xmlschema

from tests.harness.paths import cache_dir, file_lock

XSD_URL = "https://www.subsonic.org/pages/inc/api/schema/subsonic-rest-api-1.16.1.xsd"
XSD_SHA256 = "9baee72fa24087190ad22be4f7674d628acc7a3e0fb65160d74115b166df0387"
XS = "http://www.w3.org/2001/XMLSchema"
SUBSONIC = "http://subsonic.org/restapi"

# Per complex type: (attributes as name/type, elements as name/type/maxOccurs), in the order
# Navidrome writes them.
MANY = "unbounded"
ADDITIONS: dict[str, tuple[list[tuple[str, str]], list[tuple[str, str, str]]]] = {
    "Response": (
        [("type", "xs:string"), ("serverVersion", "xs:string"), ("openSubsonic", "xs:boolean")],
        [],
    ),
    "Child": (
        [
            ("name", "xs:string"),
            ("songCount", "xs:int"),
            ("played", "xs:dateTime"),
            ("bpm", "xs:int"),
            ("comment", "xs:string"),
            ("sortName", "xs:string"),
            ("mediaType", "sub:OsMediaType"),
            ("musicBrainzId", "xs:string"),
            ("channelCount", "xs:int"),
            ("samplingRate", "xs:int"),
            ("bitDepth", "xs:int"),
            ("displayArtist", "xs:string"),
            ("displayAlbumArtist", "xs:string"),
            ("displayComposer", "xs:string"),
            ("explicitStatus", "sub:ExplicitStatus"),
        ],
        [
            ("isrc", "xs:string", MANY),
            ("genres", "sub:ItemGenre", MANY),
            ("replayGain", "sub:ReplayGain", "1"),
            ("moods", "xs:string", MANY),
            ("artists", "sub:ArtistID3Ref", MANY),
            ("albumArtists", "sub:ArtistID3Ref", MANY),
            ("contributors", "sub:Contributor", MANY),
            ("groupings", "xs:string", MANY),
            ("works", "sub:Work", MANY),
            ("movements", "sub:Movement", MANY),
        ],
    ),
    "AlbumID3": (
        [
            ("played", "xs:dateTime"),
            ("userRating", "sub:UserRating"),
            ("averageRating", "sub:AverageRating"),
            ("musicBrainzId", "xs:string"),
            ("isCompilation", "xs:boolean"),
            ("sortName", "xs:string"),
            ("displayArtist", "xs:string"),
            ("explicitStatus", "sub:ExplicitStatus"),
            ("version", "xs:string"),
        ],
        [
            ("genres", "sub:ItemGenre", MANY),
            ("discTitles", "sub:DiscTitle", MANY),
            ("originalReleaseDate", "sub:ItemDate", "1"),
            ("releaseDate", "sub:ItemDate", "1"),
            ("releaseTypes", "xs:string", MANY),
            ("recordLabels", "sub:RecordLabel", MANY),
            ("moods", "xs:string", MANY),
            ("artists", "sub:ArtistID3Ref", MANY),
        ],
    ),
    "ArtistID3": (
        [
            ("userRating", "sub:UserRating"),
            ("averageRating", "sub:AverageRating"),
            ("musicBrainzId", "xs:string"),
            ("sortName", "xs:string"),
        ],
        [("roles", "xs:string", MANY)],
    ),
    "Artist": ([("coverArt", "xs:string")], []),
    "Directory": (
        [
            ("played", "xs:dateTime"),
            ("artist", "xs:string"),
            ("artistId", "xs:string"),
            ("coverArt", "xs:string"),
            ("songCount", "xs:int"),
            ("albumCount", "xs:int"),
            ("duration", "xs:int"),
            ("created", "xs:dateTime"),
            ("year", "xs:int"),
            ("genre", "xs:string"),
        ],
        [],
    ),
}
NEW_TYPES = """
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:sub="http://subsonic.org/restapi">
  <xs:simpleType name="OsMediaType">
    <xs:restriction base="xs:string">
      <xs:enumeration value="song"/><xs:enumeration value="album"/>
      <xs:enumeration value="artist"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="ExplicitStatus">
    <xs:restriction base="xs:string">
      <xs:enumeration value="explicit"/><xs:enumeration value="clean"/>
      <xs:enumeration value=""/>
    </xs:restriction>
  </xs:simpleType>
  <xs:complexType name="ItemGenre">
    <xs:attribute name="name" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:complexType name="RecordLabel">
    <xs:attribute name="name" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:complexType name="ArtistID3Ref">
    <xs:attribute name="id" type="xs:string" use="required"/>
    <xs:attribute name="name" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:complexType name="Contributor">
    <xs:sequence>
      <xs:element name="artist" type="sub:ArtistID3Ref" minOccurs="1" maxOccurs="1"/>
    </xs:sequence>
    <xs:attribute name="role" type="xs:string" use="required"/>
    <xs:attribute name="subRole" type="xs:string" use="optional"/>
  </xs:complexType>
  <xs:complexType name="ReplayGain">
    <xs:attribute name="trackGain" type="xs:double" use="optional"/>
    <xs:attribute name="albumGain" type="xs:double" use="optional"/>
    <xs:attribute name="trackPeak" type="xs:double" use="optional"/>
    <xs:attribute name="albumPeak" type="xs:double" use="optional"/>
    <xs:attribute name="baseGain" type="xs:double" use="optional"/>
    <xs:attribute name="fallbackGain" type="xs:double" use="optional"/>
  </xs:complexType>
  <xs:complexType name="ItemDate">
    <xs:attribute name="year" type="xs:int" use="optional"/>
    <xs:attribute name="month" type="xs:int" use="optional"/>
    <xs:attribute name="day" type="xs:int" use="optional"/>
  </xs:complexType>
  <xs:complexType name="DiscTitle">
    <xs:attribute name="disc" type="xs:int" use="required"/>
    <xs:attribute name="title" type="xs:string" use="required"/>
    <xs:attribute name="coverArt" type="xs:string" use="optional"/>
  </xs:complexType>
  <xs:complexType name="Work">
    <xs:attribute name="name" type="xs:string" use="required"/>
    <xs:attribute name="musicBrainzId" type="xs:string" use="optional"/>
  </xs:complexType>
  <xs:complexType name="Movement">
    <xs:attribute name="name" type="xs:string" use="required"/>
    <xs:attribute name="number" type="xs:int" use="optional"/>
    <xs:attribute name="count" type="xs:int" use="optional"/>
  </xs:complexType>
</xs:schema>
"""


def subsonic_xsd() -> bytes:
    """The Subsonic 1.16.1 schema, downloaded once and checked."""
    target = cache_dir() / "subsonic" / "subsonic-rest-api-1.16.1.xsd"
    if not target.exists():
        with file_lock(target.parent / ".lock"):
            if not target.exists():
                response = httpx.get(XSD_URL, follow_redirects=True, timeout=60)
                response.raise_for_status()
                if hashlib.sha256(response.content).hexdigest() != XSD_SHA256:
                    raise RuntimeError("checksum mismatch for subsonic-rest-api-1.16.1.xsd")
                partial = target.with_suffix(".partial")
                partial.write_bytes(response.content)
                partial.replace(target)
    data = target.read_bytes()
    if hashlib.sha256(data).hexdigest() != XSD_SHA256:
        raise RuntimeError("the cached subsonic-rest-api-1.16.1.xsd changed")
    return data


def _q(name: str) -> str:
    return f"{{{XS}}}{name}"


@cache
def schema() -> xmlschema.XMLSchema:
    """The schema with the OpenSubsonic additions Navidrome emits."""
    ET.register_namespace("xs", XS)
    root = ET.fromstring(subsonic_xsd())  # noqa: S314 - a checked download
    types = {t.get("name"): t for t in root.findall(_q("complexType"))}
    for name, (attributes, elements) in ADDITIONS.items():
        complex_type = types[name]
        if elements:
            assert complex_type.find(_q("sequence")) is None, name
            sequence = ET.Element(_q("sequence"))
            for element, kind, most in elements:
                sequence.append(
                    ET.Element(
                        _q("element"),
                        {"name": element, "type": kind, "minOccurs": "0", "maxOccurs": most},
                    )
                )
            complex_type.insert(0, sequence)
        for attribute, kind in attributes:
            complex_type.append(
                ET.Element(_q("attribute"), {"name": attribute, "type": kind, "use": "optional"})
            )
    for new in ET.fromstring(NEW_TYPES):  # noqa: S314 - written here
        root.append(new)
    text = ET.tostring(root, encoding="unicode")
    # The "sub:" prefix is used in attribute values only, which ElementTree does not declare.
    declared = text.replace("<xs:schema ", f'<xs:schema xmlns:sub="{SUBSONIC}" ', 1)
    return xmlschema.XMLSchema(declared)


def problems(body: bytes) -> list[str]:
    """What makes an XML answer invalid against the schema (nothing: valid)."""
    return [str(error.reason or error)[:300] for error in schema().iter_errors(body.decode())]


def valid(body: bytes) -> bool:
    found: list[Any] = problems(body)
    assert found == [], found
    return True


# --- the same data in JSON and XML (independent of Shijhon's XML writer) ----------------


def local(tag: str) -> str:
    return tag.rpartition("}")[2]


def scalar(value: Any, text: str) -> bool:
    if isinstance(value, bool):
        return text == ("true" if value else "false")
    if isinstance(value, int):
        return text == str(value)
    if isinstance(value, float):
        return float(text) == value
    return text == value


def same_data(value: dict[str, Any], element: ET.Element, where: str = "") -> list[str]:
    """What differs between a JSON object and its XML element."""
    found: list[str] = []
    attributes = dict(element.attrib)
    children: dict[str, list[ET.Element]] = {}
    for child in element:
        children.setdefault(local(child.tag), []).append(child)
    for key, json_value in value.items():
        here = f"{where}.{key}"
        if isinstance(json_value, list):
            kids = children.pop(key, [])
            if key == "roles":  # Navidrome's order of roles changes from answer to answer
                json_value, kids = sorted(json_value), sorted(kids, key=lambda k: k.text or "")
            if len(kids) != len(json_value):
                found.append(f"{here}: {len(json_value)} in JSON, {len(kids)} in XML")
                continue
            for n, (one, kid) in enumerate(zip(json_value, kids, strict=True)):
                if isinstance(one, dict):
                    found += same_data(one, kid, f"{here}[{n}]")
                elif (kid.text or "") != str(one) or kid.attrib or len(kid):
                    found.append(f"{here}[{n}]: {one!r} in JSON, {kid.text!r} in XML")
        elif isinstance(json_value, dict):
            kids = children.pop(key, [])
            if not any(json_value.values()):
                if kids:
                    found.append(f"{here}: empty in JSON, an element in XML")
            elif len(kids) != 1:
                found.append(f"{here}: {len(kids)} elements in XML")
            else:
                found += same_data(json_value, kids[0], here)
        else:
            text = attributes.pop(key, None)
            texts = children.get(key, [])
            if text is None and len(texts) == 1 and not texts[0].attrib and not len(texts[0]):
                text = children.pop(key)[0].text or ""
            if text is None:
                if json_value not in ("", 0, False, None):
                    found.append(f"{here}: {json_value!r} in JSON, not in XML")
            elif not scalar(json_value, text):
                found.append(f"{here}: {json_value!r} in JSON, {text!r} in XML")
    found += [f"{where}: attribute {k} only in XML" for k in attributes]
    found += [f"{where}: element {k} only in XML" for k in children]
    return found
