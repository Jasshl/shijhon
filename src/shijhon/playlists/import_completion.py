"""Merge newly found import entries into a native playlist the owner may have edited.

Pure functions only: the review computes a plan from a snapshot of the current
native playlist, and the publisher writes exactly that plan.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter, deque
from difflib import SequenceMatcher
from typing import Any

from shijhon.playlists.model import PlaylistError
from shijhon.views.ids import CatalogId

# Up to this many entries a completion replaces the playlist's tracks in one native
# write. Navidrome refuses requests with 10,000 or more query parameters.
ONE_STEP_LIMIT = 5000
# Bound on SequenceMatcher work for one gap between anchors (it is quadratic, worse with
# repeated IDs): a few milliseconds at this size. Larger gaps pair repeats in order.
ALIGN_WORK = 10_000
CHANGED = "Your playlist changed since this review. Press Recheck to review again."


def missing_entries(rows: list[dict[str, Any]]) -> list[int]:
    """Entry numbers still without a recording; owner removals are not missing."""
    return [i + 1 for i, r in enumerate(rows) if r["state"] not in ("matched", "removed")]


def published_entries(saved: dict[str, Any]) -> tuple[list[str], list[int | None]]:
    """The last published native IDs and the import entry number of each one."""
    ids: list[str] = saved.get("playlist_ids") or saved["native_ids"]
    rows: list[int | None] | None = saved.get("entry_rows")
    if rows is None:
        # Publications before completion merges contained exactly the matched rows.
        rows = [i + 1 for i, r in enumerate(saved["rows"]) if r["state"] == "matched"]
    if len(rows) != len(ids):
        raise PlaylistError("The published recording positions could not be verified.")
    return ids, rows


def anchors(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """Positions of songs that occur exactly once in both lists, as many as keep their
    order in both (a longest increasing subsequence, O(n log n))."""
    in_a, in_b = Counter(a), Counter(b)
    where = {value: j for j, value in enumerate(b) if in_b[value] == 1}
    pairs = [(i, where[v]) for i, v in enumerate(a) if in_a[v] == 1 and v in where]
    tails: list[int] = []  # for each length, the pair ending the best run so far
    ends: list[int] = []  # that pair's position in ``b``
    before = [-1] * len(pairs)
    for k, (_, j) in enumerate(pairs):
        length = bisect_left(ends, j)
        if length:
            before[k] = tails[length - 1]
        if length == len(tails):
            tails.append(k)
            ends.append(j)
        else:
            tails[length], ends[length] = k, j
    found: list[tuple[int, int]] = []
    k = tails[-1] if tails else -1
    while k >= 0:
        found.append(pairs[k])
        k = before[k]
    return found[::-1]


def align(previous: list[str], rows: list[int | None], current: list[str]) -> list[int | None]:
    """Map each current entry to the import entry it came from, or None for the owner's.

    Songs that occur once in both lists anchor the alignment in their common order; the
    small gaps between anchors are matched closely, larger ones pair repeated songs in
    order. Remaining entries with the same ID are paired in order, so a moved song
    keeps its import entry. Unpaired previous entries were removed by the owner.
    """
    n, m = len(previous), len(current)
    result: list[int | None] = [None] * m
    used, paired = [False] * n, [False] * m

    def link(i: int, j: int) -> None:
        result[j] = rows[i]
        used[i] = paired[j] = True

    start = 0
    while start < min(n, m) and previous[start] == current[start]:
        link(start, start)
        start += 1
    end = 0
    while end < min(n, m) - start and previous[n - 1 - end] == current[m - 1 - end]:
        link(n - 1 - end, m - 1 - end)
        end += 1
    a, b = previous[start : n - end], current[start : m - end]
    fixed = anchors(a, b)
    for (i0, j0), (i1, j1) in zip([(-1, -1), *fixed], [*fixed, (len(a), len(b))], strict=True):
        if i1 < len(a):
            link(start + i1, start + j1)
        gap_a, gap_b = a[i0 + 1 : i1], b[j0 + 1 : j1]
        if gap_a and gap_b and len(gap_a) * len(gap_b) <= ALIGN_WORK:
            matcher = SequenceMatcher(None, gap_a, gap_b, autojunk=False)
            for i, j, size in matcher.get_matching_blocks():
                for k in range(size):
                    link(start + i0 + 1 + i + k, start + j0 + 1 + j + k)
    left: dict[str, deque[int]] = {}
    for i, value in enumerate(previous):
        if not used[i] and rows[i] is not None:
            left.setdefault(value, deque()).append(i)
    for j, value in enumerate(current):
        if not paired[j] and left.get(value):
            link(left[value].popleft(), j)
    return result


def merge(
    snapshot: list[dict[str, Any]], rows: list[dict[str, Any]], missing: list[int]
) -> list[dict[str, Any]]:
    """Place each newly matched entry next to its original neighbours.

    Each new entry goes right after the nearest earlier import entry still in the
    playlist (including entries placed earlier in this merge); without one, before
    the nearest later entry; without any, at the end. A song the owner already added
    is used instead of adding a second copy. Every other entry stays in its order.
    """
    present = sorted((s["row"], j) for j, s in enumerate(snapshot) if "row" in s)
    present_rows = [n for n, _ in present]
    slot_of = dict(present)
    free: dict[str, deque[int]] = {}
    for j, s in enumerate(snapshot):
        if "row" not in s:
            free.setdefault(s["id"], deque()).append(j)
    found: dict[int, int] = {}
    before: dict[int, list[int]] = {}
    after: dict[int, list[int]] = {}
    end: list[int] = []
    last: int | None = None
    last_group: list[int] | None = None
    for n in sorted(missing):
        row = rows[n - 1]
        if row["state"] != "matched":
            continue
        song = str(row["track"]["id"])
        if CatalogId.parse(song) is None and free.get(song):
            j = free[song].popleft()
            found[j] = n
            last, last_group = n, after.setdefault(j, [])
            continue
        k = bisect_left(present_rows, n)
        earlier = present_rows[k - 1] if k else None
        if last is not None and last_group is not None and (earlier is None or last > earlier):
            group = last_group
        elif earlier is not None:
            group = after.setdefault(slot_of[earlier], [])
        elif present_rows:
            group = before.setdefault(slot_of[present_rows[0]], [])
        else:
            group = end
        group.append(n)
        last, last_group = n, group
    plan: list[dict[str, Any]] = []
    for j, s in enumerate(snapshot):
        plan.extend({"row": n, "add": True} for n in before.get(j, []))
        if j in found:
            plan.append({"id": s["id"], "row": found[j], "found": True})
        elif "row" in s:
            plan.append({"id": s["id"], "row": s["row"]})
        else:
            plan.append(dict(s))
        plan.extend({"row": n, "add": True} for n in after.get(j, []))
    plan.extend({"row": n, "add": True} for n in end)
    return plan


def staged(plan: list[dict[str, Any]]) -> bool:
    return len(plan) > ONE_STEP_LIMIT


def materialize(
    plan: list[dict[str, Any]], rows: list[dict[str, Any]], native_ids: list[str]
) -> tuple[list[str], list[int | None]]:
    """The reviewed plan as native IDs, after its recordings were prepared."""
    matched = [i + 1 for i, r in enumerate(rows) if r["state"] == "matched"]
    if len(matched) != len(native_ids):
        raise PlaylistError("The prepared recordings do not match this review.")
    prepared = dict(zip(matched, native_ids, strict=True))
    target: list[str] = []
    entry_rows: list[int | None] = []
    for slot in plan:
        if "row" in slot:
            native = prepared.get(slot["row"], "")
            if not native or ("id" in slot and slot["id"] != native):
                raise PlaylistError("A reviewed recording changed. Press Recheck to review again.")
            target.append(native)
            entry_rows.append(slot["row"])
        else:
            target.append(slot["id"])
            entry_rows.append(None)
    return target, entry_rows


def staged_entries(
    original: list[str], target: list[str], prefix: int, appended: int, removed: int
) -> list[str]:
    """Large lists: old entries stay until the complete replacement suffix is present."""
    return original[:prefix] + original[prefix + removed :] + target[prefix:appended]


def song(slot: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    data = rows[slot["row"] - 1]["track"] if "row" in slot else slot
    return " · ".join(v for v in (data.get("artist", ""), data.get("title", "")) if v)


def changes(completion: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The planned additions in plain words, in playlist order."""
    plan = completion.get("plan") or []
    result = []
    for t, slot in enumerate(plan):
        if slot.get("add"):
            result.append(
                {
                    "kind": "add",
                    "song": song(slot, rows),
                    "after": song(plan[t - 1], rows) if t else "",
                }
            )
        elif slot.get("found"):
            result.append({"kind": "found", "song": song(slot, rows), "after": ""})
    return result
