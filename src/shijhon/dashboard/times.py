"""Times as the dashboard prints them.

The server writes each time in its own zone inside a ``<time>`` element that carries the
exact instant; ``static/times.js`` writes it again in the browser's zone, in the same style.
Without JavaScript the server's text stays.
"""

from __future__ import annotations

import time

from markupsafe import Markup

STYLES = ("hm", "day", "when")


def hm_text(at: float) -> str:
    """14:05"""
    return time.strftime("%H:%M", time.localtime(at))


def day_text(at: float) -> str:
    """12 September"""
    moment = time.localtime(at)
    return f"{moment.tm_mday} {time.strftime('%B', moment)}"


def when_text(at: float) -> str:
    """14:05 today, else 12 September, 14:05."""
    today = time.localtime().tm_yday == time.localtime(at).tm_yday and abs(time.time() - at) < 86400
    return hm_text(at) if today else f"{day_text(at)}, {hm_text(at)}"


def instant(at: float) -> str:
    """The moment in UTC, as a ``datetime`` attribute takes it."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def _element(at: float, style: str, text: str) -> Markup:
    return Markup('<time datetime="{}" data-time="{}">{}</time>').format(instant(at), style, text)


def hm(at: float) -> Markup:
    return _element(at, "hm", hm_text(at))


def day(at: float) -> Markup:
    return _element(at, "day", day_text(at))


def when(at: float) -> Markup:
    return _element(at, "when", when_text(at))
