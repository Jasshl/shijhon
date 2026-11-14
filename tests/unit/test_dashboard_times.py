"""Times on the dashboard: the server's text in a <time> element with the exact instant, and
the script that writes it in the browser's zone."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from shijhon.dashboard import times

ROOT = Path(__file__).parents[2]
BROWSER_ZONE = "Asia/Kathmandu"  # (+05:45: no server's zone by chance)
MONTHS = "January February March April May June July August September October November December"


def test_older_times_carry_their_day() -> None:
    now = time.time()
    assert re.fullmatch(r"\d\d:\d\d", times.when_text(now))
    assert re.fullmatch(r"\d{1,2} \w+, \d\d:\d\d", times.when_text(now - 3 * 86400))


@pytest.mark.parametrize("style", times.STYLES)
def test_a_time_is_an_element_with_its_instant_and_the_servers_text(style: str) -> None:
    at = 1_762_950_300.9  # 2025-11-12 12:25:00.9 UTC
    shown = getattr(times, style)(at)
    text = getattr(times, f"{style}_text")(at)
    assert str(shown) == (
        f'<time datetime="2025-11-12T12:25:00Z" data-time="{style}">{text}</time>'
    )


def _in_browser(cases: list[list[str]]) -> list[str]:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the browser script check.")
    found = subprocess.run(
        [
            node,
            str(Path(__file__).with_name("fixtures") / "dashboard_times_browser.cjs"),
            str(ROOT / "src/shijhon/dashboard/static"),
            json.dumps(cases),
        ],
        check=True,
        timeout=30,
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": BROWSER_ZONE},
    )
    result: list[str] = json.loads(found.stdout)
    return result


def test_the_script_writes_times_in_the_browsers_zone_in_the_servers_style() -> None:
    now = time.time()
    earlier = now - 3 * 86400
    cases = [
        [times.instant(now), "hm", times.hm_text(now)],
        [times.instant(earlier), "day", times.day_text(earlier)],
        [times.instant(now), "when", times.when_text(now)],
        [times.instant(earlier), "when", times.when_text(earlier)],
    ]
    shown = _in_browser(cases)

    def local(at: float) -> datetime:
        return datetime.fromtimestamp(int(at), ZoneInfo(BROWSER_ZONE))

    def day(at: float) -> str:
        return f"{local(at).day} {MONTHS.split()[local(at).month - 1]}"

    today = local(now).strftime("%H:%M")
    assert shown[0] == today
    assert shown[1] == day(earlier)
    assert shown[2] == today  # (today in the browser's zone)
    assert shown[3] == f"{day(earlier)}, {local(earlier).strftime('%H:%M')}"


def test_the_script_leaves_what_it_cannot_read() -> None:
    cases = [
        ["not a time", "hm", "14:05"],
        ["2025-11-12T12:25:00Z", "weekday", "12 November"],
    ]
    assert _in_browser(cases) == ["14:05", "12 November"]


# --- the sidebar's counts -----------------------------------------------------------------


@pytest.mark.anyio
async def test_a_count_that_cannot_be_read_is_left_out_and_none_shows_for_nothing() -> None:
    """The sidebar's counts never break a page: one whose read fails is left out, the other
    still shows; a count of nothing shows nothing."""
    from types import SimpleNamespace

    from shijhon.dashboard.app import Dashboard

    class Store:
        def __init__(self, review: int | Exception) -> None:
            self.review = review

        async def fetchone(self, sql: str, params: object = ()) -> dict[str, int]:
            if isinstance(self.review, Exception):
                raise self.review
            return {"n": self.review}

    async def two_addons() -> list[SimpleNamespace]:
        return [SimpleNamespace(id=1), SimpleNamespace(id=2)]

    async def broken() -> list[SimpleNamespace]:
        raise OSError("database is locked")

    def page(review: int | Exception, stored: object, signing_in: bool) -> SimpleNamespace:
        services = SimpleNamespace(
            fills=SimpleNamespace(scope="demo:"), sources=SimpleNamespace(stored=stored)
        )
        return SimpleNamespace(
            services=services, store=Store(review), _signing_in=lambda addon: signing_in
        )

    attention = Dashboard._attention
    assert await attention(page(3, two_addons, True)) == {  # type: ignore[arg-type]
        "library": (3, "3 albums to review"),
        "addons": (2, "2 add-ons need sign-in"),
    }
    assert await attention(page(OSError("busy"), two_addons, True)) == {  # type: ignore[arg-type]
        "addons": (2, "2 add-ons need sign-in")
    }
    assert await attention(page(1, broken, True)) == {  # type: ignore[arg-type]
        "library": (1, "1 album to review")
    }
    assert await attention(page(0, two_addons, False)) == {}  # type: ignore[arg-type]
