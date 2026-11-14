"""When the library pass reads an added release's track list again (``fill.track_lists``):
about once a month, once a week while the release is new, a day after a failure, at once
for what a dry run found once the pass is on - and only a share of them a run."""

from __future__ import annotations

from datetime import UTC, datetime

from shijhon.fill.track_lists import (
    DAY,
    MIN_PER_RUN,
    NEW_SECONDS,
    OLD_SECONDS,
    RETRY_SECONDS,
    UNWRITTEN_SECONDS,
    Entry,
    due,
    due_at,
    is_new,
    per_run,
)

NOW = datetime(2031, 3, 15, 12, tzinfo=UTC).timestamp()
RUN = 6 * 3600.0


def entry(ref: str = "demo:1", **fields: object) -> Entry:
    values: dict[str, object] = {
        "album_id": "album",
        "added_at": NOW - 400 * DAY,
        "release_date": "2010-05-06",
        **fields,
    }
    return Entry(ref, **values)  # type: ignore[arg-type]


def test_a_release_is_new_for_two_months_after_its_date_and_before_it() -> None:
    assert is_new("2031-03-15", NOW)
    assert is_new("2031-01-14", NOW)  # 60 days back
    assert not is_new("2031-01-13", NOW)
    assert is_new("2031-09-01", NOW)  # not released yet
    assert is_new("2031-01", NOW)  # a month counts to its end
    assert not is_new("2030-12", NOW)
    assert is_new("2030-12", NOW - 30 * DAY)  # (December rolls into the next year)
    for unknown in ("2031", None, "", "soon", "2031-13", "2031-02-31"):
        assert not is_new(unknown, NOW)


def test_a_release_is_first_read_again_an_interval_after_it_was_added() -> None:
    added = NOW - 10 * DAY
    assert due_at(entry(added_at=added), NOW, apply=True) == added + OLD_SECONDS
    new = entry(added_at=added, release_date="2031-03-01")
    assert due_at(new, NOW, apply=True) == added + NEW_SECONDS


def test_a_release_is_read_again_an_interval_after_its_last_reading() -> None:
    read = NOW - 3 * DAY
    for outcome in ("same", "changed", "refused", "gone"):
        old = entry(checked_at=read, outcome=outcome)
        assert due_at(old, NOW, apply=True) == read + OLD_SECONDS
    new = entry(checked_at=read, outcome="same", release_date="2031-04-01")
    assert due_at(new, NOW, apply=False) == read + NEW_SECONDS


def test_a_failed_reading_is_tried_again_a_day_later() -> None:
    failed = entry(checked_at=NOW - 3600, outcome="failed")
    assert due_at(failed, NOW, apply=True) == NOW - 3600 + RETRY_SECONDS
    assert due([failed], NOW, apply=True, run_seconds=RUN) == ([], 0)
    assert due([failed], NOW + DAY, apply=True, run_seconds=RUN) == ([failed], 0)


def test_changes_that_could_not_be_written_are_tried_again_a_week_later() -> None:
    unwritten = entry(checked_at=NOW - 2 * DAY, outcome="unwritten", release_date="2031-03-01")
    assert due_at(unwritten, NOW, apply=True) == NOW - 2 * DAY + UNWRITTEN_SECONDS
    assert due([unwritten], NOW, apply=True, run_seconds=RUN) == ([], 0)


def test_what_a_dry_run_found_is_read_again_at_once_when_the_pass_is_on() -> None:
    pending = entry(checked_at=NOW - 3600, outcome="pending")
    assert due([pending], NOW, apply=True, run_seconds=RUN) == ([pending], 0)
    # ... and a dry run keeps to the interval.
    assert due([pending], NOW, apply=False, run_seconds=RUN) == ([], 0)


def test_a_run_reads_the_longest_due_first_and_only_its_share() -> None:
    """A library whose releases all came due together is read over many runs."""
    entries = [entry(f"demo:{n}", added_at=NOW - (40 + n) * DAY) for n in range(3000)]
    limit = per_run(entries, NOW, RUN)
    assert limit == 50  # twice what keeps up with 3000 a month at four runs a day
    todo, more = due(entries, NOW, apply=True, run_seconds=RUN)
    assert [e.ref for e in todo] == [f"demo:{n}" for n in range(2999, 2999 - limit, -1)]
    assert more == 3000 - limit
    # All of them are read within the month at that share.
    assert limit * (OLD_SECONDS / RUN) >= len(entries)


def test_a_small_library_is_read_a_few_releases_a_run() -> None:
    entries = [entry(f"demo:{n}") for n in range(45)]
    assert per_run(entries, NOW, RUN) == MIN_PER_RUN
    todo, more = due(entries, NOW, apply=True, run_seconds=RUN)
    assert (len(todo), more) == (MIN_PER_RUN, 45 - MIN_PER_RUN)


def test_new_releases_count_for_their_weekly_readings() -> None:
    old = [entry(f"demo:{n}") for n in range(3000)]
    new = [entry(f"demo:n{n}", release_date="2031-03-01") for n in range(3000)]
    assert per_run(old + new, NOW, RUN) > 4 * per_run(old, NOW, RUN)


def test_nothing_is_due_before_its_time() -> None:
    entries = [
        entry("demo:1", added_at=NOW - 29 * DAY),
        entry("demo:2", checked_at=NOW - 29 * DAY, outcome="changed"),
        entry("demo:3", added_at=NOW - 6 * DAY, release_date="2031-03-10"),
    ]
    assert due(entries, NOW, apply=True, run_seconds=RUN) == ([], 0)
    todo, _ = due(entries, NOW + DAY, apply=True, run_seconds=RUN)
    assert {e.ref for e in todo} == {"demo:1", "demo:2", "demo:3"}
