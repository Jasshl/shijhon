"""Name normalization shared by matching, search de-duplication and add-on checks.

Case, accents, punctuation and spacing are ignored ("IntoTheQuietHarbor" equals
"Into The Quiet Harbor"); edition markers and "feat." credits are dropped from titles;
artist credits split into individual names.

An edition marker says the release is an edition of the same album: remastered, deluxe,
anniversary, expanded, bonus tracks, "… Edition", explicit or clean, in brackets or as a
short dash suffix ("Album - 2011 Remaster"). Other suffixes name different recordings and
stay part of an album's title: "(Artist's Version)", "(2019 Mix)", "(Live)".

Track titles (``recording_key``): a version marker on one side
only means other audio - "(Live)", "(Acoustic Version)", "(Instrumental)", "(Demo)",
"(Remix)", "- Radio Edit", "(Mono)", "(Spanish Version)", "(Album Version)", "(Single
Version)", "(Mara's Version)" and any other suffix stay part of the title ("Version"
aside: "(Live Version)" is "(Live)"). Edition markers name the same audio: a remaster ("-
2011 Remaster", "(Remastered 2009 Version)"), a deluxe, expanded, anniversary or other
edition ("(40th Anniversary Edition)", "(Special Edition)"), a bonus track; they go, also
from a suffix that says more ("(Live - 2011 Remaster)" is "(Live)"). "(Clean)" and
"(Explicit)" go too: the clean rule compares them (``version_marker``). Credits are
not versions: "feat. X" (up to the next qualifier) and a bracketed "(with X)" go.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

_MARKER_WORDS = r"remaster(?:ed)?|deluxe|edition|explicit|clean|bonus|anniversary|expanded"


def _bracketed(words: str) -> re.Pattern[str]:
    return re.compile(rf"[\(\[][^\)\]]*\b(?:{words})\b[^\)\]]*[\)\]]", re.IGNORECASE)


def _dashed(words: str) -> re.Pattern[str]:
    """A dash suffix of a few plain words around a marker: "Song - Remastered 2009",
    "Album - 20th Anniversary Edition" (not "Greatest Hits - Volume 1 (Remastered)")."""
    return re.compile(
        rf"\s+-\s+(?:[\w']+\s+){{0,3}}(?:{words})(?:\s+[\w']+){{0,3}}\s*$", re.IGNORECASE
    )


_MARKER = _bracketed(_MARKER_WORDS)
_DASH_MARKER = _dashed(_MARKER_WORDS)
# Title-only kind markers in brackets: "(Single Version)", "[EP]".
_KIND = _bracketed(r"single|ep")
_FEAT = re.compile(r"[\(\[]?\s*\b(feat\.?|featuring|ft\.)\s+[^\)\]]*[\)\]]?", re.IGNORECASE)
_KIND_SUFFIX = re.compile(r"\s+-\s+(single|ep)\s*$", re.IGNORECASE)
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\bx\b|;|/|\bfeat\.?|\bfeaturing\b|\bft\.?)\s*", re.I)


def fold(text: str | None) -> str:
    """Lower case, no accents, letters and digits only (spaces removed)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in plain.casefold() if ch.isalnum())


def title_key(title: str | None) -> str:
    """An album title without edition markers, feat. credits or " - Single"/" - EP"
    suffixes."""
    text = _FEAT.sub(" ", _KIND_SUFFIX.sub("", title or ""))
    text = _DASH_MARKER.sub("", text)
    text = _MARKER.sub(" ", text)
    text = _KIND.sub(" ", text)
    return fold(text) or fold(title)


def name_key(title: str | None) -> str:
    """A single's or EP's title: only a " - Single"/" - EP" suffix is dropped."""
    return fold(_KIND_SUFFIX.sub("", title or "")) or fold(title)


def edition_marked(title: str | None) -> bool:
    """Whether the title carries an edition marker ("(Remastered)", "[Deluxe Edition]")."""
    text = _FEAT.sub(" ", _KIND_SUFFIX.sub("", title or ""))
    return bool(_MARKER.search(text) or _DASH_MARKER.search(text))


_VERSION = r"(clean|explicit)(?:\s+(?:version|edit|edition|lyrics|content))?"
# Only a bracket or dash suffix that names the version, nothing else in it: "(Clean)",
# "[Explicit Version]", "- Clean Edit"; not "(with Clean Lanterns)" or "(feat. Ada Clean)".
_VERSION_MARK = re.compile(
    rf"[\(\[]\s*{_VERSION}\s*[\)\]]|\s+-\s+{_VERSION}\s*(?=$|[\(\[])", re.IGNORECASE
)


def version_marker(title: str | None) -> str | None:
    """ "clean" or "explicit" when the title says which version it is ("Song (Clean)",
    "Song [Explicit Version]"), else None: a clean edit is other audio."""
    match = _VERSION_MARK.search(title or "")
    return match.group(1 if match.group(1) else 2).lower() if match else None


# A track title's qualifiers: each bracket, then a dash suffix (after the brackets went).
_BRACKET = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_DASH_SUFFIX = re.compile(r"\s+[-\u2013\u2014]\s+(.+)$")
# A credit, not a version: "(feat. X)", "(with X)", or an unbracketed "feat. X" up to the
# next qualifier ("Song feat. X (Live)" keeps its "(Live)").
_CREDIT = re.compile(
    r"[\(\[]\s*(?:feat\.?|featuring|ft\.?|with)\s+[^\)\]]*[\)\]]"
    r"|\s+(?:feat\.?|featuring|ft\.)\s+.*?(?=\s+[-\u2013\u2014]\s+|\s*[\(\[]|$)",
    re.IGNORECASE,
)
# The parts of one qualifier: "(Live - 2011 Remaster)", "[2019 Remix & Remaster]".
_PARTS = re.compile(r"\s+[-\u2013\u2014]\s+|\s*[/,;&]\s*")
# Edition markers: a part with one names the same audio - a remaster, a deluxe,
# expanded, anniversary or other edition, a bonus track.
_EDITION = {"remaster", "remastered", "remasters", "remastering", "deluxe", "expanded",
            "anniversary", "bonus", "reissue", "reissued", "edition"}  # fmt: skip
_REMASTERED = {"remaster", "remastered", "remasters", "remastering"}  # a year next to it goes
# An edition's label ("(Japan Bonus Track)", "(Tour Edition)"): only a version word in it
# says more about the audio.
_LABELS = {"bonus", "edition"}
_VERSION_WORDS = {"live", "acoustic", "demo", "remix", "remixed", "mix", "edit", "instrumental",
                  "mono", "stereo", "single", "album", "radio", "extended", "unplugged",
                  "orchestral", "piano", "karaoke", "acapella", "cappella", "dub", "stripped",
                  "session", "sessions", "rehearsal", "take", "alternate", "alt", "original",
                  "reprise", "cover", "instrumentals", "remixes"}  # fmt: skip
# Words that go with an edition marker ("Super Deluxe", "Special Edition", "Version").
_COMPANIONS = {"digital", "digitally", "super", "special", "legacy", "limited", "collector's",
               "collectors", "version", "track", "the", "of", "and"}  # fmt: skip
_FILLERS = {"version", "edition", "track", "the", "of", "and"}  # nothing on their own
_NUMBER = re.compile(r"\d+(?:st|nd|rd|th)?")
_WORDS = re.compile(r"[\w'\u2019]+")
_CLEAN_OR_EXPLICIT = re.compile(rf"\s*{_VERSION}\s*", re.IGNORECASE)


def _part_words(part: str) -> list[str]:
    """A qualifier's part as a recording title keeps it: "version" goes from any
    part ("(Live Version)" is "(Live)"); an edition's label keeps only its version words;
    an edition marker goes with its companion words and a year next to a remaster word, and
    a part of nothing else goes whole; nothing is kept of a part left with fillers only."""
    words = [w.replace("\u2019", "'") for w in _WORDS.findall(part.lower())]
    if not _EDITION.intersection(words):
        words = [w for w in words if w != "version"]
    elif _LABELS.intersection(words):
        words = [w for w in words if w in _VERSION_WORDS]
    else:
        pure = all(w in _EDITION or w in _COMPANIONS or _NUMBER.fullmatch(w) for w in words)
        gone = {i for i, w in enumerate(words) if w in _EDITION or w in _COMPANIONS}
        near = _EDITION if pure else _REMASTERED  # "40th Anniversary", "2011 Remaster"
        for i, w in enumerate(words):
            beside = {words[j] for j in (i - 1, i + 1) if 0 <= j < len(words)}
            if _NUMBER.fullmatch(w) and beside & near:
                gone.add(i)
        words = [w for i, w in enumerate(words) if i not in gone]
    return [] if set(words) <= _FILLERS else words


def _same_audio(replacement: str) -> Callable[[re.Match[str]], str]:
    """A qualifier as a recording title keeps it: "(Clean)" goes (the clean rule compares
    it), as does an edition marker - also from a qualifier that says more ("(Live - 2011
    Remaster)" is "(Live)"); any other marker stays."""

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if _CLEAN_OR_EXPLICIT.fullmatch(inner):
            return replacement  # the clean rule compares these
        parts = [words for part in _PARTS.split(inner) if (words := _part_words(part))]
        if _EDITION.intersection(_WORDS.findall(inner.lower())):  # "(2021 - Remaster)"
            parts = [w for w in parts if not all(_NUMBER.fullmatch(x) for x in w)]
        if not parts:
            return replacement
        return " (" + " / ".join(" ".join(words) for words in parts) + ")"

    return replace


_QUALIFIERS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]|\s+-\s+.*$")


def core_title(title: str | None) -> str:
    """The title without bracketed or dash-suffixed qualifiers and feat. credits, spaces
    kept: "Stay (feat. X)" -> "Stay" (a search term that finds every version)."""
    core = _QUALIFIERS.sub("", _FEAT.sub(" ", title or "")).strip()
    return core if len(core) >= 2 else (title or "").strip()


def recording_key(title: str | None) -> str:
    """A track title for comparing recordings: edition markers, "(Clean)" and
    credits go; any other version marker stays ("(Live)", "(Spanish Version)", "- Radio
    Edit"), the same marker on both sides being the same ("(Live Version)", "- Live")."""
    text = _CREDIT.sub(" ", _KIND_SUFFIX.sub("", title or ""))
    text = _BRACKET.sub(_same_audio(" "), text)
    text = _DASH_SUFFIX.sub(_same_audio(""), text)
    return fold(text) or fold(title)


def artist_names(credit: str | None) -> set[str]:
    """Individual folded names in a credit such as "A feat. B & C"."""
    parts = _ARTIST_SPLIT.split(credit or "")
    return {fold(p) for p in parts if fold(p)} | ({fold(credit)} if fold(credit) else set())


def credit_names(credit: str | None) -> list[str]:
    """The individual names of a credit such as "A feat. B & C", in order, as written."""
    return [p.strip() for p in _ARTIST_SPLIT.split(credit or "") if p and p.strip()]


_FEATURING = re.compile(r"(?<=\S)\s*[\(\[]?\s*\b(?:feat\.?|featuring|ft\.?)(?=\s)\s*", re.I)


def featured_names(credit: str | None) -> list[str]:
    """A credit split on "feat." alone ("A feat. B" -> A, B): band names such as "Salt,
    Ash & Ember" or "Quill & Marrow" stay whole."""
    parts = [p.strip().rstrip(")]").strip() for p in _FEATURING.split(credit or "")]
    return [p for p in parts if p]


def same_artist(a: str | None, b: str | None) -> bool:
    """True if the two credits share at least one artist."""
    return bool(artist_names(a) & artist_names(b))


def same_title(a: str | None, b: str | None) -> bool:
    """The same recording's title (``recording_key``)."""
    return recording_key(a) == recording_key(b)


def close_duration(a_ms: int | None, b_ms: int | None, tolerance_ms: int = 3000) -> bool:
    if not a_ms or not b_ms:
        return False
    return abs(a_ms - b_ms) <= tolerance_ms
