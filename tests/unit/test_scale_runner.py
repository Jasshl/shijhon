"""The scale runner's synthetic catalog and statistics (suite O, ``tools/scale.py``)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import anyio
import pytest
from tools.scale import (
    cover,
    drop_unrecorded,
    plan,
    queries,
    run_folder,
    scan_times,
    scrub_environment,
    summary,
)

from shijhon.navidrome.scans import ScanCoordinator


@pytest.mark.parametrize("count", [1, 17, 500])
def test_the_plan_has_exactly_the_placeholders_asked_for(count: int) -> None:
    releases, _ = plan(count, seed=1)
    assert sum(len(r.tracks) for r in releases) == count
    assert all(r.tracks and r.track_count == len(r.tracks) for r in releases)


def test_the_plan_is_the_same_for_a_seed_and_its_items_are_distinct() -> None:
    first, generator = plan(2000, seed=7)
    again, _ = plan(2000, seed=7)
    assert first == again
    assert plan(2000, seed=8)[0] != first
    tracks = [t for r in first for t in r.tracks]
    assert len({r.ref for r in first}) == len(first)
    assert len({t.ref for t in tracks}) == len({t.isrc for t in tracks}) == len(tracks)
    # A few releases per artist, about ten tracks a release, 2-5.5 minutes a track.
    assert 2 <= len(first) / len(generator.artists) <= 8
    assert 8 <= len(tracks) / len(first) <= 13
    assert all(120_000 <= t.duration_ms <= 330_000 for t in tracks)
    for release in first:
        positions = [(t.disc, t.number) for t in release.tracks]
        assert len(set(positions)) == len(positions)


def test_more_releases_continue_the_plan_without_repeating_it() -> None:
    releases, generator = plan(100, seed=1)
    extra = [generator.release(tracks=12) for _ in range(3)]
    assert all(len(r.tracks) == 12 for r in extra)
    assert not {r.ref for r in extra} & {r.ref for r in releases}


def _segments(data: bytes) -> list[int]:
    """The markers of a JPEG's segments up to the image data."""
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"
    at, markers = 2, []
    while True:
        assert data[at] == 0xFF
        marker = data[at + 1]
        markers.append(marker)
        if marker == 0xDA:  # start of scan: the image data follows
            return markers
        at += 2 + int.from_bytes(data[at + 2 : at + 4], "big")


@pytest.mark.parametrize("size", [0, 300, 70_000, 131_071, 131_072, 320_000])
def test_covers_are_jpegs_of_the_size_asked_for(size: int) -> None:
    data = cover(size, seed=3)
    assert size - 3 <= len(data) <= max(size, 202)
    assert 0xDA in _segments(data)
    assert cover(size, seed=3) == data


def test_search_terms_are_distinct_and_of_every_kind() -> None:
    _, generator = plan(500, seed=1)
    terms = queries(generator, 40, seed=2)
    assert len(terms) == 40
    assert len({t.lower() for t in terms}) == 40
    assert any(t in generator.artists for t in terms)
    assert any(len(t) == 3 for t in terms)


def test_summary_uses_the_nearest_rank() -> None:
    values = [float(n) for n in range(1, 101)]
    stats = summary(values)
    assert (stats["n"], stats["min"], stats["p50"], stats["p95"], stats["max"]) == (
        100,
        1.0,
        50.0,
        95.0,
        100.0,
    )
    assert summary([0.25], 1000)["p95"] == 250.0
    assert summary([]) == {"n": 0}


def test_search_terms_come_to_the_count_asked_for_in_a_small_library() -> None:
    _, generator = plan(3, seed=1)
    assert len(set(queries(generator, 12, seed=2))) == 12


# --- the run folder: nothing outside it is written or removed ---------------------------

FINGERPRINT = {"count": 10, "seed": 1, "cover_kb": 0, "navidrome": "0.64.2"}


def test_a_new_run_folder_is_made_and_an_existing_one_only_resumed(tmp_path: Path) -> None:
    with run_folder(tmp_path, FINGERPRINT, resume=False) as root:
        assert root == tmp_path / "shijhon-scale"
        assert json.loads((root / "plan.json").read_text()) == FINGERPRINT
    with pytest.raises(SystemExit, match="--resume"), run_folder(tmp_path, FINGERPRINT, False):
        pass
    with run_folder(tmp_path, FINGERPRINT, resume=True) as again:
        assert again == root
    other = {**FINGERPRINT, "count": 11}
    with pytest.raises(SystemExit, match="was started with"), run_folder(tmp_path, other, True):
        pass


def test_a_run_folder_in_use_or_behind_a_link_is_refused(tmp_path: Path) -> None:
    with (
        run_folder(tmp_path, FINGERPRINT, resume=False),
        pytest.raises(SystemExit, match="in use"),
        run_folder(tmp_path, FINGERPRINT, resume=True),
    ):
        pass
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "shijhon-scale" / "navidrome").symlink_to(elsewhere)
    with pytest.raises(SystemExit, match="symbolic link"), run_folder(tmp_path, FINGERPRINT, True):
        pass
    (tmp_path / "shijhon-scale" / "navidrome").unlink()
    deep = tmp_path / "shijhon-scale" / "shijhon"
    deep.mkdir()
    (deep / "shijhon.sqlite3").symlink_to(elsewhere / "other.sqlite3")
    with pytest.raises(SystemExit, match="symbolic link"), run_folder(tmp_path, FINGERPRINT, True):
        pass
    (deep / "shijhon.sqlite3").unlink()
    hidden = deep / "hidden"
    hidden.mkdir(mode=0o300)  # can be entered, not listed
    try:
        with (
            pytest.raises(SystemExit, match="cannot check"),
            run_folder(tmp_path, FINGERPRINT, True),
        ):
            pass
    finally:
        hidden.chmod(0o700)
    lock = tmp_path / ".shijhon-scale.lock"
    lock.unlink()
    lock.symlink_to(tmp_path / "shijhon-scale" / "plan.json")
    with pytest.raises(SystemExit, match="cannot use"), run_folder(tmp_path, FINGERPRINT, True):
        pass
    lock.unlink()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "shijhon-scale").symlink_to(tmp_path / "shijhon-scale")
    with pytest.raises(SystemExit, match="symbolic link"), run_folder(linked, FINGERPRINT, True):
        pass


def test_an_interrupted_run_s_unrecorded_releases_are_removed_on_resume(tmp_path: Path) -> None:
    folder = tmp_path / "navidrome" / "music" / "_shijhon"
    for name in ("Kalo Ren/Done [1]", "Kalo Ren/Half [2]", "Vel Tu/Also Done [3]"):
        (folder / name).mkdir(parents=True)
        (folder / name / "1-01 Song.flac").write_bytes(b"")
    (folder / ".staging").mkdir()
    (tmp_path / "shijhon").mkdir()
    db = sqlite3.connect(tmp_path / "shijhon" / "shijhon.sqlite3")
    db.execute("CREATE TABLE releases (folder TEXT)")
    db.executemany(
        "INSERT INTO releases VALUES (?)",
        [("_shijhon/Kalo Ren/Done [1]",), ("_shijhon/Vel Tu/Also Done [3]",)],
    )
    db.commit()
    db.close()
    assert drop_unrecorded(tmp_path) == ["_shijhon/Kalo Ren/Half [2]"]
    assert sorted(p.name for p in (folder / "Kalo Ren").iterdir()) == ["Done [1]"]
    assert (folder / ".staging").is_dir() and (folder / "Vel Tu" / "Also Done [3]").is_dir()
    assert drop_unrecorded(tmp_path / "nothing-here") == []


def test_shijhon_s_settings_are_removed_from_the_environment() -> None:
    environ = {
        "SHIJHON_CATALOG__TOKEN": "x",
        "SHIJHON_LOG_LEVEL": "DEBUG",
        "SHIJHON_TEST_CACHE": "/cache",
        "SHIJHON_NAVIDROME_VERSION": "latest",
        "HOME": "/home",
    }
    assert scrub_environment(environ) == ["SHIJHON_CATALOG__TOKEN", "SHIJHON_LOG_LEVEL"]
    assert sorted(environ) == ["HOME", "SHIJHON_NAVIDROME_VERSION", "SHIJHON_TEST_CACHE"]


class _Navidrome:
    """Scans that end at once, as the scan coordinator sees them."""

    def __init__(self) -> None:
        self.scans = 0

    async def start_scan(self, folders: Any) -> dict[str, Any]:
        self.scans += 1
        return {}

    async def scan_status(self) -> dict[str, Any]:
        return {"scanning": False, "lastScan": str(self.scans)}


def test_the_scan_coordinator_s_timing_line_is_read() -> None:
    coordinator = ScanCoordinator(_Navidrome(), poll_seconds=0.01)  # type: ignore[arg-type]
    with scan_times() as scans:
        anyio.run(coordinator.scan, ["_shijhon/A/B [1]"])
    assert len(scans.seconds) == 1
