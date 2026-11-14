"""Whether a catalog's list of artists is exactly what a file's joined artist text says.

A file that names several artists in one text ("Lumen Vale, Orrin Pike") gets the list
only when the catalog's names, in the catalog's order, consume that whole text from left
to right, with nothing but separators between them: ``,`` ``&`` ``;`` ``/`` ``+`` and the
words "and", "x" and "with", and spaces. Letter case, Unicode compatibility forms and runs
of spaces aside; an escaped comma ("Tyler Quill\\, The Maker") is a comma. The text is
never split on its own: "Tyler Quill, The Maker & Kali Fen" is two artists only for a
catalog that names "Tyler Quill, The Maker" and "Kali Fen".

Nothing else counts: at least two distinct names, none of them "Various Artists"; nothing
left over (a featured artist the text does not name, or a name in another script, is no
match); no separator alone between two names that are not there. What is written is the
file's own spelling of each name, as its text has it (a run of spaces one space).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

# Separators between two names: punctuation (also without spaces around it) and words
# (between spaces).
_PUNCTUATION = frozenset(",&;/+")
_WORDS = ("and", "x", "with")
_ESCAPED = re.compile(r"\\([,;&/+])")
_NOT_ARTISTS = frozenset({"variousartists"})


def _unescaped(text: str) -> str:
    return _ESCAPED.sub(r"\1", unicodedata.normalize("NFC", text))


def _folded(text: str) -> tuple[str, list[int]]:
    """The text compared (each character in its compatibility form, case folded, any run
    of spaces one space) and, for each of its characters, the index of the character of
    ``text`` it comes from."""
    out: list[str] = []
    origin: list[int] = []
    for index, ch in enumerate(text):
        if ch.isspace():
            if out and out[-1] != " ":
                out.append(" ")
                origin.append(index)
            continue
        for piece in unicodedata.normalize("NFKC", ch).casefold():
            out.append(" " if piece.isspace() else piece)
            origin.append(index)
    while out and out[-1] == " ":
        out.pop()
        origin.pop()
    return "".join(out), origin


def _key(name: str) -> str:
    return "".join(ch for ch in _folded(_unescaped(name))[0] if ch.isalnum())


def _starts(text: str, at: int) -> list[int]:
    """Where the next name may start after the separators (and spaces) from ``at``: every
    place past at least one separator that is not a space (a name may itself begin with a
    separator's character: "+44"); none without a separator (spaces alone are none)."""
    positions: list[int] = []
    position, seen = at, False
    while position < len(text):
        ch = text[position]
        if seen and ch != " " and position not in positions:
            positions.append(position)
        if ch == " ":
            position += 1
            continue
        if ch in _PUNCTUATION:
            position, seen = position + 1, True
            continue
        word = next(
            (
                w
                for w in _WORDS
                if text.startswith(w, position)
                and position > 0
                and text[position - 1] == " "
                and position + len(w) < len(text)
                and text[position + len(w)] == " "
            ),
            None,
        )
        if word is None:
            break
        position, seen = position + len(word), True
    if seen and position < len(text) and text[position] != " " and position not in positions:
        positions.append(position)
    return positions


def _ends_name(text: str, at: int) -> bool:
    """Whether a name may end before ``at``: the text ends, or a space or a punctuation
    separator follows."""
    return at == len(text) or text[at] == " " or text[at] in _PUNCTUATION


def listed(text: str, names: Sequence[str]) -> list[str] | None:
    """The file's own spelling of each of the catalog's ``names`` when they are exactly
    its joined artist ``text`` (module docstring); else None."""
    wanted = [_folded(_unescaped(n).strip())[0] for n in names]
    keys = [_key(n) for n in names]
    if (
        len(wanted) < 2
        or not all(wanted)
        or not all(keys)
        or len(set(keys)) != len(keys)
        or _NOT_ARTISTS.intersection(keys)
    ):
        return None
    original = _unescaped(text)
    folded, origin = _folded(original)

    def consume(index: int, position: int) -> list[str] | None:
        name = wanted[index]
        end = position + len(name)
        if not folded.startswith(name, position) or not _ends_name(folded, end):
            return None
        spelled = " ".join(original[origin[position] : origin[end - 1] + 1].split())
        if index == len(wanted) - 1:
            return [spelled] if end == len(folded) else None
        for start in _starts(folded, end):
            rest = consume(index + 1, start)
            if rest is not None:
                return [spelled, *rest]
        return None

    return consume(0, 0) if folded else None


def maybe_joined(text: str) -> bool:
    """Whether an artist text may name several artists: a separator between two words of
    it (``listed`` decides)."""
    folded, _ = _folded(_unescaped(text))
    return any(
        (folded[position] == " " or folded[position] in _PUNCTUATION)
        and folded[:position].strip()
        and _starts(folded, position)
        for position in range(1, len(folded))
    )
