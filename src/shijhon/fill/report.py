"""``shijhon matches``: the album matches as a text report - what the library pass's dry run
would do, the review list and the rest, per catalog and region. It reads the database
without changing it (the server may be running). The dashboard's Library page shows the review list.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

_ORDER = ("filled", "complete", "review", "none", "deferred", "failed", "kept")
_NAMES = {
    "filled": "filled",
    "complete": "complete",
    "review": "for review",
    "none": "without a match",
    "deferred": "to fill on first use",
    "failed": "failed",
    "kept": "kept as they are",
}


class ReportError(Exception):
    """The database cannot be read (the message says why)."""


def report(database: Path, *, everything: bool = False, policy: Any = None) -> str:
    """The report; ``everything`` also lists the albums without a match and the complete
    ones (otherwise only counted). With the fill ``policy``, what an automatic fill would
    fill is what it allows now: a dry run's plans, and albums kept to fill on first use
    (as the Library page counts them - which counts only the albums in the library now,
    and only the running catalog's). Raises ``ReportError``."""
    if not database.is_file():
        raise ReportError(f"no database at {database}")
    try:
        conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT scope, album_id, outcome, planned, release_ref, reason, candidates,"
                " title, artist, plan, owned_songs, release_tracks, cleaned_at,"
                " album_id IN (SELECT owned_album_id FROM releases WHERE owned_album_id IS NOT"
                " NULL) AS has_release FROM album_matches ORDER BY scope, artist, title, album_id"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ReportError(f"cannot read {database}: {exc}") from None
    if not rows:
        return "No album has been matched yet."
    lines: list[str] = []
    for scope in dict.fromkeys(row["scope"] for row in rows):
        lines += _scope(scope, [r for r in rows if r["scope"] == scope], everything, policy)
    return "\n".join(lines)


def _allowed(row: Any, policy: Any) -> bool:
    """A plan an automatic fill would follow now (never one whose fill the cleanup took
    out)."""
    if row["has_release"]:
        return False  # filled already (under another catalog or region)
    if policy is None:
        return row["outcome"] == "filled" and bool(row["planned"])
    if row["cleaned_at"] is not None:
        return False
    return bool(policy.allows(int(row["owned_songs"] or 0), int(row["release_tracks"] or 0)))


def _scope(scope: str, found: list[sqlite3.Row], everything: bool, policy: Any) -> list[str]:
    # An album with a release is filled (also under another catalog), whatever its match.
    rows = [
        {**dict(r), "outcome": "filled", "planned": 0} if r["has_release"] else dict(r)
        for r in found
    ]
    done = [r for r in rows if not r["planned"]]
    planned = [r for r in rows if r["planned"]]
    would = [r for r in planned if r["outcome"] == "filled" and _allowed(r, policy)]
    earlier = [r for r in rows if r["outcome"] == "deferred" and policy and _allowed(r, policy)]
    listed = {r["album_id"] for r in would + earlier}
    lines = [f"{scope}: {len(rows)} album(s) checked"]
    if done:
        lines.append("  " + _counts(done))
    if planned:
        added = sum(_added(r) for r in planned if r["outcome"] == "filled")
        lines.append(
            f"  dry run, not acted on yet: {_counts(planned)}; {added} placeholder(s) to add"
        )
    if policy is not None:  # the counts above are the outcomes as recorded
        share = f"{policy.min_share * 100:g}"
        lines.append(
            f"  an automatic fill would fill {len(would) + len(earlier)} album(s) now (the fill"
            f" policy: {policy.min_songs} songs or {share} %)"
        )
    sections = [
        ("Would fill (dry run)", would),
        ("Would fill (matched earlier; the fill policy allows it now)", earlier),
        ("For review (dry run)", [r for r in planned if r["outcome"] == "review"]),
        ("For review", [r for r in done if r["outcome"] == "review"]),
        ("Filled", [r for r in done if r["outcome"] == "filled"]),
        ("Failed", [r for r in rows if r["outcome"] == "failed"]),
        ("Kept as they are", [r for r in done if r["outcome"] == "kept"]),
    ]
    if everything:
        sections += [
            (
                "To fill on first use",
                [r for r in rows if r["outcome"] == "deferred" and r["album_id"] not in listed]
                + [r for r in planned if r["outcome"] == "filled" and r["album_id"] not in listed],
            ),
            ("Complete", [r for r in rows if r["outcome"] == "complete"]),
            ("Without a match", [r for r in rows if r["outcome"] == "none"]),
        ]
    for heading, entries in sections:
        if entries:
            lines += ["", f"{heading}:"]
            lines += [f"  {_line(r)}" for r in entries]
    lines.append("")
    return lines


def _counts(rows: list[Any]) -> str:
    counts = Counter(r["outcome"] for r in rows)
    return ", ".join(f"{_NAMES[o]} {counts[o]}" for o in _ORDER if counts[o])


def _album(row: Any) -> str:
    if row["title"]:
        return f"{row['artist'] or '?'} - {row['title']}"
    return f"album {row['album_id']}"


def _plan(row: Any) -> dict[str, Any]:
    try:
        data = json.loads(row["plan"] or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _added(row: Any) -> int:
    plan = _plan(row)
    release = plan.get("release") or {}
    return max(0, len(release.get("tracks") or []) - len(plan.get("links") or {}))


def _line(row: Any) -> str:
    album = _album(row)
    outcome = row["outcome"]
    if outcome == "filled" and row["planned"]:
        release = _plan(row).get("release") or {}
        total = len(release.get("tracks") or [])
        return (
            f"{album}: {_added(row)} of {total} tracks to add from {row['release_ref']}"
            f" ({release.get('title') or '?'})"
        )
    text = f"{album}: {row['reason'] or outcome}"
    if outcome == "filled":
        text = f"{album}: from {row['release_ref']}"
    candidates = [c for c in str(row["candidates"] or "").split(",") if c]
    if outcome in ("review", "kept") and candidates:
        text += f" [candidates: {', '.join(candidates)}]"
    return text
