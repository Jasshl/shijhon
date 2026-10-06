from __future__ import annotations

from shijhon.matching.normalize import (
    artist_names,
    close_duration,
    core_title,
    edition_marked,
    fold,
    same_artist,
    same_title,
    title_key,
    version_marker,
)


def test_fold_ignores_case_accents_punctuation_and_spacing() -> None:
    assert fold("IntoTheQuietHarbor") == fold("Into The Quiet Harbor") == "intothequietharbor"
    assert fold("Velané") == fold("VELANE")


def test_title_key_drops_editions_feat_and_kind_suffix() -> None:
    assert title_key("Glass Orchard (Remastered 2021)") == title_key("Glass Orchard")
    assert title_key("Song (feat. Someone)") == title_key("Song")
    assert title_key("Song - Single") == title_key("Song")
    assert title_key("Song [Deluxe Edition]") == title_key("Song")
    assert title_key("Song (Live)") != title_key("Song")
    assert title_key("Song - Remastered 2009") == title_key("Song")
    assert title_key("Song (Bonus Track Version)") == title_key("Song")


def test_only_edition_markers_are_editions() -> None:
    for title in (
        "A (Remastered)",
        "A [Deluxe Version]",
        "A (20th Anniversary Edition)",
        "A (Expanded)",
        "A (Bonus Track Version)",
        "A - 2011 Remaster",
    ):
        assert edition_marked(title), title
    for title in ("A (Artist's Version)", "A (2019 Mix)", "A (Live)", "A [Radio Edit]", "A"):
        assert not edition_marked(title), title
    assert title_key("A (Artist's Version)") != title_key("A")
    assert title_key("A (2019 Mix)") != title_key("A")
    assert not edition_marked("A (feat. Ada Clean)")


def test_a_dash_suffix_is_an_edition_only_when_short_and_plain() -> None:
    assert title_key("Hits - Volume 1 (Remastered)") != title_key("Hits - Volume 2 (Remastered)")
    assert title_key("X - Ray (Deluxe Edition)") == title_key("X - Ray")
    assert title_key("Album - 20th Anniversary Edition") == title_key("Album")
    assert edition_marked("Album - Remastered 2009")


def test_recording_titles() -> None:
    """A version marker on one side only is other audio; only edition markers -
    remaster, deluxe, expanded, anniversary, other editions, bonus track - (and the clean
    rule's "(Clean)"/"(Explicit)", compared apart) are the same audio."""
    assert not same_title(
        "Suite - I. Allegro (2015 Remaster)", "Suite - II. Andante (2015 Remaster)"
    )
    for other in ("Song (Live)", "Song (Live Version)", "Song (Mono Version)", "Song (Mono)",
                  "Song (Acoustic Version)", "Song - Radio Edit", "Song (Instrumental)",
                  "Song (Demo)", "Song (Remix)", "Song - Single Edit", "Song (Edit)",
                  "Song (Spanish Version)", "Song (Alternate Version)", "Song (Album Version)",
                  "Song (Single Version)", "Song (2019 Mix)", "Song (Original Mix)",
                  "Song (2011)", "Song (Mono Remaster)",
                  "Song (Live - Deluxe Edition)"):  # fmt: skip
        assert not same_title(other, "Song"), other
    assert not same_title("Song (Single Version)", "Song (Album Version)")
    assert not same_title("Open Door (Mara's Version)", "Open Door")  # a re-recording
    assert same_title("Open Door (Mara\u2019s Version)", "Open Door (Mara's Version)")
    for same in ("Song (Remastered)", "Song - 2011 Remaster", "Song (2011 Remastered Version)",
                 "Song - Remastered 2009 Version", "Song [2009 Digital Remaster]",
                 "Song (40th Anniversary Remastered Edition)", "Song (Clean)",
                 "Song (Deluxe Edition)", "Song (40th Anniversary Edition)", "Song (Expanded)",
                 "Song [Bonus Track]", "Song (Special Edition)", "Song (Deluxe Version)",
                 "Song - Anniversary Edition",
                 "Song [Explicit Version]", "Song (feat. Guest) [Remastered]",
                 "Song \u2013 Remastered", "Song (with Guest)", "Song feat. Guest",
                 "Song (2021 - Remaster)", "Song - Remastered / 2009"):  # fmt: skip
        assert same_title(same, "Song"), same
    # The same marker on both sides is the same audio, "Version" or not.
    for a, b in (("Song (Live Version)", "Song (Live)"),
                 ("Song (Acoustic Version)", "Song - Acoustic"),
                 ("Song (Instrumental Version)", "Song (Instrumental)")):  # fmt: skip
        assert same_title(a, b), (a, b)
    # A remaster marker goes from a qualifier that says more; the rest still counts.
    assert same_title("Song (Live - 2011 Remaster)", "Song (Live)")
    assert same_title("Song (Remastered Live Version)", "Song (Live Version)")
    assert same_title("Song (Remastered 2009) [Mono]", "Song (Mono)")
    assert same_title("Song [2019 Remix & Remaster]", "Song (2019 Remix)")
    assert same_title("Song - Live - 2011 Remaster", "Song - Live")
    # ... and nothing else with it: numbers of the rest stay.
    for a, b in (("Song (Part 1 - 2011 Remaster)", "Song (Part 2 - 2011 Remaster)"),
                 ("Song - Take 2 - Remastered", "Song - Take 3 - Remastered"),
                 ("Song (Live at Wembley 1986 - Remastered)", "Song (Live at Wembley 1985)"),
                 ("Song (Demo 1 / Remastered)", "Song (Demo 2 / Remastered)"),
                 ("Song (Remastered Take 2)", "Song (Take 3)")):  # fmt: skip
        assert not same_title(a, b), (a, b)
    # A credit hides no version marker after it.
    for credited in ("Song feat. Guest (Live)", "Song ft. Guest - Live",
                     "Song featuring Guest - Radio Edit"):  # fmt: skip
        assert not same_title(credited, "Song"), credited
    assert same_title("Song feat. Guest (Live)", "Song (Live)")


def test_edition_words_inside_other_words() -> None:
    """An edition's label ("Japan Bonus Track", "Tour Edition") is the same audio
    whatever else it names, but a version word in it counts; elsewhere an edition word goes
    and the rest stays - numbers too, but a remaster's year."""
    for same, other in (
        ("Song (Japan Bonus Track)", "Song"),
        ("Song (Store Bonus Track)", "Song"),
        ("Song (Tour Edition)", "Song"),
        ("Song (Collectors Edition)", "Song"),
        ("Song (Japanese Edition)", "Song"),
        ("Song (Special Edition Live)", "Song (Live)"),
        ("Song (Collector's Edition Demo)", "Song (Demo)"),
        ("Song (Deluxe Mono)", "Song (Mono)"),
        ("Song (2019 Anniversary Remix)", "Song (2019 Remix)"),
    ):
        assert same_title(same, other), (same, other)
    for one, two in (
        (
            "Song (Live at the 25th Anniversary Concert)",
            "Song (Live at the 30th Anniversary Concert)",
        ),
        ("Song (Live at the 25th Anniversary Concert)", "Song (Live at Concert)"),
        ("Song (Live - 25th Anniversary Tour)", "Song (Live - 30th Anniversary Tour)"),
        ("Song (25th Anniversary Remix)", "Song (Remix)"),
        ("Song (Anniversary Remix)", "Song"),
        ("Song (Japanese Bonus Track)", "Song (Japanese Version)"),
    ):
        assert not same_title(one, two), (one, two)


def test_version_markers() -> None:
    assert version_marker("Song (Clean)") == "clean"
    assert version_marker("Song [Clean Version]") == "clean"
    assert version_marker("Song - Clean") == "clean"
    assert version_marker("Song (Explicit)") == "explicit"
    assert version_marker("Song (feat. Ada Clean)") is None
    assert version_marker("Clean Living") is None
    assert version_marker("Song") is None
    assert version_marker("Song feat. X (Clean)") == "clean"  # a credit hides nothing
    assert version_marker("Song ft. X - Clean Edit") == "clean"
    assert version_marker("Song (with Clean Lanterns)") is None
    assert version_marker("Song (Clean Lanterns Remix)") is None


def test_core_title_is_a_search_term_for_every_version() -> None:
    assert core_title("Stay (feat. Oren Vale)") == "Stay"
    assert core_title("Deep Cut (2011 Remaster)") == "Deep Cut"
    assert core_title("Song - Radio Edit") == "Song"
    assert core_title("(Untitled)") == "(Untitled)"  # nothing left: the whole title


def test_artist_credits() -> None:
    assert artist_names("A feat. B & C") >= {"a", "b", "c"}
    assert same_artist("Lead, Guest", "Guest")
    assert not same_artist("Someone", "Other")


def test_durations() -> None:
    assert close_duration(180_000, 182_900)
    assert not close_duration(180_000, 183_500)
    assert not close_duration(None, 180_000)
