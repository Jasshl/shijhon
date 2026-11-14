"""Whether a catalog's names are exactly a file's joined artist text (``names.listed``)."""

from __future__ import annotations

import pytest

from shijhon.artist_lists.names import listed, maybe_joined

WIDE = "\uff2c\uff55\uff4d\uff45\uff4e"  # "Lumen" in full-width letters


@pytest.mark.parametrize(
    ("text", "names", "expected"),
    [
        ("Lumen Vale, Orrin Pike", ["Lumen Vale", "Orrin Pike"], ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale & Orrin Pike", ["Lumen Vale", "Orrin Pike"], ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale and Orrin Pike", ["Lumen Vale", "Orrin Pike"], ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale x Orrin Pike", ["Lumen Vale", "Orrin Pike"], ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale; Orrin Pike", ["Lumen Vale", "Orrin Pike"], ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale/Orrin Pike", ["Lumen Vale", "Orrin Pike"], ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale + Orrin Pike", ["Lumen Vale", "Orrin Pike"], ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale with Orrin Pike", ["Lumen Vale", "Orrin Pike"], ["Lumen Vale", "Orrin Pike"]),
        # Three names, an Oxford comma, separators without spaces.
        ("Ash, Bell, and Cora", ["Ash", "Bell", "Cora"], ["Ash", "Bell", "Cora"]),
        ("Ash,Bell&Cora", ["Ash", "Bell", "Cora"], ["Ash", "Bell", "Cora"]),
        # A name with a comma of its own, as the catalog names it.
        (
            "Tyler Quill, The Maker & Kali Fen",
            ["Tyler Quill, The Maker", "Kali Fen"],
            ["Tyler Quill, The Maker", "Kali Fen"],
        ),
        # ... also escaped, as some taggers write it.
        (
            "Tyler Quill\\, The Maker & Kali Fen",
            ["Tyler Quill, The Maker", "Kali Fen"],
            ["Tyler Quill, The Maker", "Kali Fen"],
        ),
        # Letter case and compatibility forms aside; the file's own spelling is written.
        ("LUMEN vale, orrin PIKE", ["Lumen Vale", "Orrin Pike"], ["LUMEN vale", "orrin PIKE"]),
        (WIDE + ", Orrin", ["Lumen", "Orrin"], [WIDE, "Orrin"]),
        ("Lumen   Vale ,  Orrin Pike ", ["Lumen Vale", "Orrin Pike"], ["Lumen Vale", "Orrin Pike"]),
        # A name that begins like a separator word, or with a separator's character.
        ("Quill & X Harbor", ["Quill", "X Harbor"], ["Quill", "X Harbor"]),
        ("Quill & And Then", ["Quill", "And Then"], ["Quill", "And Then"]),
        ("Quill + +44 Echo", ["Quill", "+44 Echo"], ["Quill", "+44 Echo"]),
        ("Quill & Withers", ["Quill", "Withers"], ["Quill", "Withers"]),
    ],
)
def test_names_that_are_exactly_the_text_are_listed(
    text: str, names: list[str], expected: list[str]
) -> None:
    assert listed(text, names) == expected


@pytest.mark.parametrize(
    ("text", "names"),
    [
        # Never split on the text's own separators: the catalog's names decide.
        ("Tyler Quill, The Maker & Kali Fen", ["Tyler Quill", "Kali Fen"]),
        ("Salt, Ash & Ember", ["Salt, Ash & Ember"]),
        # Spaces alone separate nothing.
        ("Lumen Vale Orrin Pike", ["Lumen Vale", "Orrin Pike"]),
        # Not a separator: "feat." (Navidrome splits it itself), "vs.", a dash.
        ("Lumen Vale feat. Orrin Pike", ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale vs. Orrin Pike", ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale - Orrin Pike", ["Lumen Vale", "Orrin Pike"]),
        # A separator word inside a word.
        ("LumenxOrrin", ["Lumen", "Orrin"]),
        ("Lumenand Orrin", ["Lumen", "Orrin"]),
        # Another order than the catalog's.
        ("Orrin Pike & Lumen Vale", ["Lumen Vale", "Orrin Pike"]),
        # Something left over: a featured artist the text does not name, or one too few.
        ("Lumen Vale", ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale & Orrin Pike", ["Lumen Vale", "Orrin Pike", "Juno Reef"]),
        ("Lumen Vale & Orrin Pike & Juno Reef", ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale & Orrin Pike (Live)", ["Lumen Vale", "Orrin Pike"]),
        # A separator at either end.
        ("& Lumen Vale & Orrin Pike", ["Lumen Vale", "Orrin Pike"]),
        ("Lumen Vale & Orrin Pike &", ["Lumen Vale", "Orrin Pike"]),
        # One name, the same name twice, "Various Artists", an empty name.
        ("Lumen Vale", ["Lumen Vale"]),
        ("Lumen & lumen", ["Lumen", "lumen"]),
        ("Various Artists & Lumen", ["Various Artists", "Lumen"]),
        ("Lumen & ", ["Lumen", ""]),
        ("", ["Lumen", "Orrin"]),
        # Another script or a transliteration is no match (left as it is).
        ("Лумен & Оррин", ["Lumen", "Orrin"]),
        # A part of a name.
        ("Lumen Valeria & Orrin", ["Lumen Vale", "Orrin"]),
    ],
)
def test_names_that_are_not_exactly_the_text_are_not(text: str, names: list[str]) -> None:
    assert listed(text, names) is None


@pytest.mark.parametrize(
    ("text", "joined"),
    [
        ("Lumen Vale, Orrin Pike", True),
        ("Lumen & Orrin", True),
        ("Lumen and Orrin", True),
        ("Lumen x Orrin", True),
        ("Lumen/Orrin", True),
        ("Tyler Quill\\, The Maker", True),
        ("Lumen Vale", False),
        ("Andromeda", False),
        ("Mix Master", False),
        ("Lumen x", False),
        ("x Lumen", False),
        ("", False),
    ],
)
def test_texts_that_may_join_several_artists(text: str, joined: bool) -> None:
    assert maybe_joined(text) is joined
