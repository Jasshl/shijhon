"""The fallbacks' order by their recent attempts: seconds a delivered song, by what
a source's check said, blended with neutral estimates until measured; "not now" last."""

from __future__ import annotations

from typing import Any, cast

from shijhon.delivery.addon import Availability
from shijhon.delivery.netpolicy import Reach
from shijhon.delivery.playback import MEASURED_SECONDS, _Checks, seconds_a_song
from shijhon.delivery.sources import RECENT_ATTEMPTS, Attempt, Source

NOW = 10 * MEASURED_SECONDS


def make(source_id: int, name: str, budget: float | None = None) -> Source:
    return Source(
        source_id, name, source_id, Reach.PUBLIC, cast(Any, None), cast(Any, None), budget=budget
    )


def attempts(source: Source, answer: str, *outcomes: tuple[bool, float], ago: float = 60) -> None:
    for delivered, seconds in outcomes:
        source.stats.recent.append(Attempt(NOW - ago, answer, delivered, seconds))


def test_unmeasured_sources_have_neutral_estimates() -> None:
    plain, preferred, worker = make(1, "Plain"), make(2, "Preferred"), make(3, "Worker", 45.0)
    assert seconds_a_song(plain, "ready", NOW, "Preferred") == (3.0, False)
    assert seconds_a_song(plain, "-", NOW, "Preferred") == (6.0, False)
    assert seconds_a_song(plain, "not now", NOW, "Preferred") == (60.0, False)
    assert seconds_a_song(preferred, "-", NOW, "Preferred") == (4.0, False)
    assert seconds_a_song(worker, "-", NOW, "Preferred") == (45.0, False)  # its own budget
    assert seconds_a_song(worker, "ready", NOW, "Preferred") == (3.0, False)


def test_misses_and_failures_count_their_time_against_the_songs_delivered() -> None:
    source = make(1, "Plain")
    attempts(source, "-", (True, 2.0))
    assert seconds_a_song(source, "-", NOW, "") == ((2.0 + 6.0) / 2, True)
    attempts(source, "-", (False, 1.0), (False, 1.0))  # two misses
    assert seconds_a_song(source, "-", NOW, "") == ((4.0 + 6.0) / 2, True)


def test_only_the_same_answer_within_a_week_counts() -> None:
    source = make(1, "Plain")
    attempts(source, "ready", (False, 2.0))
    attempts(source, "-", (True, 0.5), ago=MEASURED_SECONDS + 1)  # too old
    assert seconds_a_song(source, "-", NOW, "") == (6.0, False)
    assert seconds_a_song(source, "ready", NOW, "") == (5.0, True)


def test_the_last_attempts_only_are_kept() -> None:
    source = make(1, "Plain")
    attempts(source, "-", *[(False, 9.0)] * (RECENT_ATTEMPTS + 10))
    assert len(source.stats.recent) == RECENT_ATTEMPTS


def test_the_order_puts_not_now_last_and_breaks_ties_by_position() -> None:
    a, b, c, d, e = (make(n, name) for n, name in enumerate("ABCDE", start=1))
    attempts(a, "-", *[(False, 9.0)] * 12)  # 108 s and nothing delivered: 114 s a song
    checks = _Checks([c, d, e], lambda s, answer: seconds_a_song(s, answer, NOW, "B")[0])
    checks.answers = {c.id: Availability(False), d.id: Availability(True)}
    checks.asked = {c.id, d.id}  # e has not answered: "not now" until it does
    assert [s.name for s in checks.order([a, b, c, d, e])] == ["D", "B", "A", "C", "E"]


def test_equal_seconds_keep_the_user_s_order() -> None:
    first, second = make(1, "First"), make(2, "Second")
    checks = _Checks([], lambda s, answer: seconds_a_song(s, answer, NOW, "")[0])
    assert checks.order([second, first]) == [first, second]
