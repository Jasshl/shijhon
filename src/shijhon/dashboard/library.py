"""The Library page's data: the album matches' counts, the library pass's progress, what
its readings of added releases' track lists led to, the review list with the releases it
considered, the albums matched by an automatic pick, the filled albums (for the owner who
finds one filled from the wrong edition), albums without a match, and actions that run in
the background (a fill waits for Navidrome's scan: up to a couple of minutes) - one
album's, or the same action for many albums one after another (``Batch``).

Everything is read from ``album_matches`` (one scope: the running catalog and region)
and done through ``Fills``' public methods; nothing here matches or fills by itself. A
search narrows every list to the albums whose title and artist hold each of its words
(letter case, accents and punctuation aside).

An action for many albums acts on what the page showed (``Selections``): each list of a
rendered page is kept on the server - its albums as searched, each with the state of its
match and the release a fill would take - under a token the list's form carries. The
rows a page shows and that selection come from one read of the database (``read``). The
form's submission names the token, the worker checks every album against it under the
action's own locks, and a token starts one action only, within half an hour of its issue.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import anyio

from shijhon.fill.fills import match_state
from shijhon.matching.matcher import NUMBERED_DIFFERENTLY, SEVERAL
from shijhon.matching.normalize import fold
from shijhon.store import Store

PAGE = 25  # albums a page in each list
SEARCH_LENGTH = 100  # characters of a search that are read
# The page's lists, by the name forms and links use: which matches each one holds.
LISTS: dict[str, str] = {
    "review": "outcome = 'review'",
    "picked": "picked != '' AND outcome IN ('filled', 'deferred')",
    # Filled from a confident match or the owner's choice (the picks have their own list).
    "filled": "picked = '' AND outcome IN ('filled', 'deferred') AND EXISTS"
    " (SELECT 1 FROM releases WHERE releases.owned_album_id = album_matches.album_id)",
    "none": "outcome = 'none'",
}
_ORDER = " ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, album_id"
DESCRIBE_SECONDS = 5.0  # a page's wait for the catalog to describe candidate releases
LISTS_DESCRIBE_SECONDS = 0.5  # ... the lists alone (a search as it is typed)
DESCRIBED_SECONDS = 86400.0
UNDESCRIBED_SECONDS = 900.0  # a release the catalog could not describe: asked again later
JOB_SHOWN_SECONDS = 900.0  # a finished action's notice
SELECTION_SECONDS = 1800.0  # how long a page's lists can be acted on as it showed them
# A list shown again unchanged keeps its token only while that has this much of its life
# left: a page just rendered is never handed one about to end.
SELECTION_REUSED_SECONDS = SELECTION_SECONDS / 2
SELECTIONS_KEPT = 30  # a viewer's lists kept at most (the oldest go first)

# The matcher's and the fills' reasons, as a headline and a sentence (others: shown as
# they are).
_REASONS: dict[str, tuple[str, str]] = {
    SEVERAL: (
        "Several releases match",
        "More than one edition fits the owned songs.",
    ),
    "the owned tracks are only on releases with other titles": (
        "Only on releases with other titles",
        "The owned songs are on releases under other names, such as a compilation.",
    ),
    NUMBERED_DIFFERENTLY: (
        "Track numbers differ",
        "The release numbers its tracks differently from your files.",
    ),
    "only a larger edition matches; the owned album may be complete": (
        "Only a larger edition matches",
        "Your album may be complete as it is.",
    ),
    "the catalog's track list could not be read in full": (
        "The track list is incomplete",
        "The catalog's track list could not be read in full.",
    ),
    "the release is in the library as another album": (
        "Already in the library",
        "The release is in the library as another album.",
    ),
    "the placeholders did not join the album": (
        "The fill did not join the album",
        "The added songs did not join your album after Navidrome's scan.",
    ),
}
# Why a release was picked automatically, as a sentence.
_PICKED: dict[str, str] = {
    SEVERAL: "Several releases fit the owned songs; this is the most likely.",
    NUMBERED_DIFFERENTLY: (
        "It numbers its tracks differently from your files; the added songs keep its numbers."
    ),
}
_PART = re.compile(r"^part of (?P<album>.+?) \(album [^)]*\): (?P<state>.+)$", re.DOTALL)


def is_part(reason: str) -> bool:
    """A part of another owned album's release: never filled on its own."""
    return (reason or "").startswith("part of ")


def explain(reason: str) -> tuple[str, str, bool]:
    """(headline, sentence, whether the album is part of another's release)."""
    if is_part(reason):
        part = _PART.match(reason)
        sentence = f"{part['album']}: {part['state']}." if part else reason[len("part of ") :]
        return "Part of another album", sentence, True
    if reason in _REASONS:
        headline, sentence = _REASONS[reason]
        return headline, sentence, False
    text = (reason or "No reason given").strip()
    return text[0].upper() + text[1:], "", False


@dataclass
class Counts:
    would_fill: int = 0  # a dry run's plan, and deferred albums the fill policy allows now
    filled: int = 0
    deferred: int = 0  # shown complete, filled on first use
    complete: int = 0
    review: int = 0
    none: int = 0  # no match
    failed: int = 0
    kept: int = 0

    @property
    def checked(self) -> int:
        """Every album with an outcome, those without a match and failures included."""
        return sum(self.__dict__.values())


async def counts(store: Store, scope: str, library: set[str] | None, policy: Any = None) -> Counts:
    """The matches of the albums in the library (``library``: their IDs; None: unknown, all
    rows count), and albums filled under another catalog or region as filled. With
    the fill ``policy``, what an automatic fill would fill counts as "would fill": a dry
    run's plan or a deferred album that it allows now - the pass switched on fills it at its
    next run, and a view fills it while the pass is off - not one whose fill the cleanup
    took out (filled only on its next use); a plan it does not allow is filled on
    first use."""
    rows = await store.fetchall(
        "SELECT album_id, outcome, planned, owned_songs, release_tracks, cleaned_at"
        " FROM album_matches WHERE scope = ?",
        [scope],
    )
    filled = await store.fetchall(
        "SELECT owned_album_id FROM releases WHERE owned_album_id IS NOT NULL"
    )
    found = Counts()
    seen: set[str] = set()
    releases = {row["owned_album_id"] for row in filled}
    for row in rows:
        if library is not None and row["album_id"] not in library:
            continue  # an album no longer in the library
        seen.add(row["album_id"])
        outcome = row["outcome"]
        if row["album_id"] in releases:  # filled (also under another catalog): as it is
            found.filled += 1
        elif outcome == "filled" and row["planned"]:  # a dry run's plan: the policy decides
            if policy is None or allowed(row, policy):
                found.would_fill += 1
            else:
                found.deferred += 1
        elif outcome == "deferred" and allowed(row, policy):
            found.would_fill += 1
        elif hasattr(found, outcome):
            setattr(found, outcome, getattr(found, outcome) + 1)
    for row in filled:
        album_id = row["owned_album_id"]
        if album_id not in seen and (library is None or album_id in library):
            seen.add(album_id)
            found.filled += 1
    return found


@dataclass
class TrackLists:
    """The releases whose track lists the library pass has read again, and what each one's
    latest reading led to, taken together. A release's next reading replaces its counts:
    these are no totals over a time."""

    releases: int = 0
    added: int = 0  # tracks added as placeholders
    updated: int = 0  # lengths corrected
    left: int = 0  # differences left as they are
    would_add: int = 0  # ... as a dry run found them
    would_update: int = 0


async def track_lists(store: Store) -> TrackLists:
    row = await store.fetchone(
        "SELECT COUNT(*) AS releases,"
        " COALESCE(SUM(CASE WHEN outcome != 'pending' THEN added END), 0) AS added,"
        " COALESCE(SUM(CASE WHEN outcome != 'pending' THEN updated END), 0) AS updated,"
        " COALESCE(SUM(left_out), 0) AS left_out,"
        " COALESCE(SUM(CASE WHEN outcome = 'pending' THEN added END), 0) AS would_add,"
        " COALESCE(SUM(CASE WHEN outcome = 'pending' THEN updated END), 0) AS would_update"
        " FROM release_refreshes WHERE outcome != 'failed'"
    )
    if row is None:
        return TrackLists()
    return TrackLists(*(int(row[key]) for key in row.keys()))  # noqa: SIM118 (a Row)


def allowed(row: Any, policy: Any) -> bool:
    """A match with a plan that the fill policy allows now (not one whose fill the cleanup
    took out: never filled automatically)."""
    if policy is None or row["cleaned_at"] is not None:
        return False
    return bool(policy.allows(int(row["owned_songs"] or 0), int(row["release_tracks"] or 0)))


@dataclass
class ReviewRow:
    album_id: str
    title: str
    artist: str
    owned: int
    tracks: int
    headline: str
    sentence: str
    part: bool
    refs: list[str]  # the releases considered (the chosen one first)
    planned: bool  # from a dry run
    likely: bool = False  # the match named a most likely release (the first of ``refs``)


async def review_count(store: Store, scope: str) -> int:
    """The review list's length."""
    row = await store.fetchone(
        "SELECT COUNT(*) AS n FROM album_matches WHERE scope = ? AND outcome = 'review'",
        [scope],
    )
    return int(row["n"]) if row else 0


def search_words(text: str) -> list[str]:
    """A search's words as they are compared: folded (lower case, no accents, letters and
    digits only), and without those that fold to nothing (punctuation alone)."""
    return _words(text[:SEARCH_LENGTH])


def _words(text: str) -> list[str]:
    return [word for word in (fold(part) for part in text.split()) if word]


_NOT_WORD_ASCII = re.compile(r"[^a-z0-9\s]+")


def _searched(*texts: str) -> str:
    """An album's title and artist as a search reads them: their words folded like the
    search's, so a word is found inside a word and never across two."""
    text = " ".join(texts)
    if text.isascii():  # (the same as folding each word, several times faster)
        return " ".join(_NOT_WORD_ASCII.sub("", text.lower()).split())
    return " ".join(_words(text))


@dataclass(frozen=True, slots=True)
class Shown:
    """One album of a list as a page showed it: the state of its match then
    (``fills.match_state``), and the release a fill of it would take ("": none - the match
    names no most likely release, or the album is a part of another's)."""

    state: str
    release: str = ""


@dataclass
class NoMatchRow:
    album_id: str
    title: str
    artist: str
    owned: int
    headline: str
    sentence: str


@dataclass
class PickRow:
    album_id: str
    title: str
    artist: str
    owned: int
    tracks: int
    sentence: str  # why the release was picked
    refs: list[str]  # the picked release, then the other plausible ones
    filled: bool  # filled from it: only another release for it can be chosen
    planned: bool  # from a dry run


@dataclass
class Lists:
    """The page's lists as one read of the database found them (one read transaction):
    every album of each list an action for many can take, under the search, in the list's
    order (artist, then title); of the filled albums, which are many and have no such
    action, the page asked for and how many there are. The rows a page shows and the
    selection its forms stand for (``Shown``) are both taken from this one read, so a
    token never stands for a match the page did not show."""

    rows: dict[str, list[Any]]
    filled_rows: list[Any] = field(default_factory=list)
    filled_total: int = 0
    filled_page: int = 1

    def page(self, name: str, number: int) -> tuple[list[Any], int, int]:
        """One page of a list's rows, the list's length, and the page shown (a page past
        the end shows the last one)."""
        rows = self.rows[name]
        number = _page_shown(number, len(rows))
        return rows[(number - 1) * PAGE : number * PAGE], len(rows), number

    def shown(self, name: str) -> dict[str, Shown]:
        """The albums of a list that an action for many can take, as they were read: a
        filled pick is not among them (it has no such action)."""
        found = {}
        for row in self.rows[name]:
            if row["filled"] and name == "picked":
                continue
            fills_from = name != "none" and row["release_ref"] and not is_part(row["reason"] or "")
            found[str(row["album_id"])] = Shown(
                match_state(row, bool(row["filled"])),
                str(row["release_ref"]) if fills_from else "",
            )
        return found

    def review(self, page: int) -> tuple[list[ReviewRow], int, int]:
        """One page of the review list, the list's length, and the page shown."""
        rows, total, page = self.page("review", page)
        found = []
        for row in rows:
            headline, sentence, part = explain(row["reason"] or "")
            found.append(
                ReviewRow(
                    row["album_id"],
                    row["title"] or "Unknown album",
                    row["artist"] or "Unknown artist",
                    int(row["owned_songs"] or 0),
                    int(row["release_tracks"] or 0),
                    headline,
                    sentence,
                    part,
                    _refs(row),
                    bool(row["planned"]),
                    bool(row["release_ref"]) and not part,
                )
            )
        return found, total, page

    def no_matches(self, page: int) -> tuple[list[NoMatchRow], int, int]:
        """One page of the current scope's negative matches."""
        rows, total, page = self.page("none", page)
        found = []
        for row in rows:
            headline, sentence, _ = explain(row["reason"] or "")
            found.append(
                NoMatchRow(
                    row["album_id"],
                    row["title"] or f"Unknown album ({row['album_id']})",
                    row["artist"] or "Unknown artist",
                    int(row["owned_songs"] or 0),
                    headline,
                    sentence,
                )
            )
        return found, total, page

    def picks(self, page: int) -> tuple[list[PickRow], int, int]:
        """One page of the albums matched by an automatic pick."""
        rows, total, page = self.page("picked", page)
        found = [
            PickRow(
                row["album_id"],
                row["title"] or "Unknown album",
                row["artist"] or "Unknown artist",
                int(row["owned_songs"] or 0),
                int(row["release_tracks"] or 0),
                _PICKED.get(row["picked"], ""),
                _refs(row),
                bool(row["filled"]),
                bool(row["planned"]),
            )
            for row in rows
        ]
        return found, total, page

    def filled(self) -> tuple[list[FilledRow], int, int]:
        """The page of the filled albums that was read (filled from a confident match or
        the owner's choice), the list's length, and the page shown."""
        found = [
            FilledRow(
                row["album_id"],
                # (A match recorded without the album's names: its release's.)
                row["title"] or row["release_title"] or "Unknown album",
                row["artist"] or row["release_artist"] or "Unknown artist",
                int(row["owned_songs"] or 0),
                int(row["release_tracks"] or 0),
                _refs(row),
            )
            for row in self.filled_rows
        ]
        return found, self.filled_total, self.filled_page


def _page_shown(number: int, total: int) -> int:
    return min(max(1, number), max(1, -(-total // PAGE)))


def _refs(row: Any) -> list[str]:
    """The releases a match lists, the one it named first."""
    refs = [row["release_ref"]] if row["release_ref"] else []
    return refs + [r for r in (row["candidates"] or "").split(",") if r and r not in refs]


# The names of the release an album is filled from, for a match that has none of its own.
_RELEASE_NAMES = (
    "(SELECT title FROM releases WHERE releases.ref = album_matches.release_ref)"
    " AS release_title,"
    " (SELECT artist FROM releases WHERE releases.ref = album_matches.release_ref)"
    " AS release_artist"
)
_COLUMNS = (
    "SELECT album_id, title, artist, owned_songs, release_tracks, outcome, reason,"
    " candidates, release_ref, planned, picked, EXISTS (SELECT 1 FROM releases"
    " WHERE releases.owned_album_id = album_matches.album_id) AS filled"
)
SEARCHED = "shijhon_searched"  # the search's SQL function: 1 when every word is found


def _found(title: str | None, artist: str | None, words: str) -> int:
    searched = _searched(title or "", artist or "")
    return int(all(word in searched for word in words.split()))


async def read(store: Store, scope: str, search: str = "", filled_page: int | None = None) -> Lists:
    """The page's lists in one read transaction - one state of the database -, narrowed by
    the search: every word is in the album's title or artist. Of the filled albums their
    number, and ``filled_page`` when it is asked for. The search is tested inside SQLite,
    so the rows it leaves out are never handed over. No catalog lookup, no write, and no
    wait for the store's writer: the read has a connection of its own
    (``Store.snapshot``)."""
    words = search_words(search)
    searching, params = "", [scope]
    if words:  # (tested last: only for the rows of a list)
        searching = f" AND {SEARCHED}(title, artist, ?)"
        params.append(" ".join(words))
    bulk = [name for name in LISTS if name != "filled"]
    member = ", ".join(f"({LISTS[name]}) AS in_{name}" for name in bulk)
    listed = " OR ".join(f"({LISTS[name]})" for name in bulk)
    found: dict[str, list[Any]] = {name: [] for name in LISTS}
    async with store.snapshot() as conn:
        if words:
            await conn.create_function(SEARCHED, 3, _found, deterministic=True)
        async with conn.execute(
            f"{_COLUMNS}, {member} FROM album_matches WHERE scope = ? AND ({listed}){searching}"
            + _ORDER,
            params,
        ) as cursor:
            for row in await cursor.fetchall():
                for name in bulk:
                    if row[f"in_{name}"]:
                        found[name].append(row)
        filled = f"FROM album_matches WHERE scope = ? AND ({LISTS['filled']}){searching}"
        async with conn.execute(f"SELECT COUNT(*) {filled}", params) as cursor:
            counted = await cursor.fetchone()
        total = int(counted[0]) if counted is not None else 0
        number, rows = _page_shown(filled_page or 1, total), []
        if filled_page is not None:
            async with conn.execute(
                f"{_COLUMNS}, {_RELEASE_NAMES} {filled}{_ORDER} LIMIT ? OFFSET ?",
                [*params, PAGE, (number - 1) * PAGE],
            ) as cursor:
                rows = list(await cursor.fetchall())
    return Lists(found, rows, total, number)


@dataclass
class FilledRow:
    album_id: str
    title: str
    artist: str
    owned: int
    tracks: int
    refs: list[str]  # the release it is filled from, then the others its match listed


def release_label(release: Any) -> str:
    """ "Deluxe edition, 2020, 18 songs (clean)"."""
    songs = len(release.tracks) or release.track_count or 0
    parts = [release.title]
    if release.year:
        parts.append(str(release.year))
    if songs:
        parts.append(f"{songs} song{'s' if songs != 1 else ''}")
    label = ", ".join(parts)
    if release.clean:
        label += " (clean)"
    elif release.explicit:
        label += " (explicit)"
    return label


class Describer:
    """Words for candidate releases, from the (cached) catalog, remembered for a day. A
    page waits at most a few seconds; what is not described by then is named by its ID,
    and is described in the background for the next view."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._known: dict[str, tuple[float, str]] = {}
        self._asking: dict[str, anyio.Event] = {}  # releases being described now

    def label(self, ref: str) -> str | None:
        known = self._known.get(ref)
        return known[1] if known is not None and known[1] else None

    async def describe(
        self,
        catalog: Any,
        refs: list[str],
        *,
        wait: float | None = None,
        spawn: Callable[[Callable[[], Coroutine[Any, Any, None]]], bool] | None = None,
    ) -> None:
        """Ask the catalog about the releases not described yet: two at a time (behind
        the listeners' requests at its rate), for ``DESCRIBE_SECONDS`` at most, a failure
        remembered for a quarter of an hour. Pass the library pass's own cached catalog
        where there is one, so these answers do not push listeners' out of the shared
        cache. ``spawn``: the asking runs in the background and the caller waits ``wait``
        seconds for it (``DESCRIBE_SECONDS`` when None); a release being described for
        another page is not asked again, only waited for."""
        now = self.clock()
        wanted, under_way = [], []
        for ref in dict.fromkeys(refs):
            if (asking := self._asking.get(ref)) is not None:
                under_way.append(asking)
                continue
            known = self._known.get(ref)
            age = DESCRIBED_SECONDS if known is None or known[1] else UNDESCRIBED_SECONDS
            if known is None or now - known[0] > age:
                wanted.append(ref)
        if catalog is None or not (wanted or under_way):
            return
        limiter = anyio.Semaphore(2)

        async def one(ref: str) -> None:
            key, _, item = ref.partition(":")
            if key != getattr(catalog, "key", None) or not item:
                return
            async with limiter:
                try:
                    release = await catalog.album(item)
                except Exception:
                    self._known[ref] = (self.clock(), "")  # not asked again for a while
                    return
            self._known[ref] = (self.clock(), release_label(release))

        done = anyio.Event()

        async def run() -> None:
            try:
                with anyio.move_on_after(DESCRIBE_SECONDS):
                    async with anyio.create_task_group() as group:
                        for ref in wanted:
                            group.start_soon(one, ref)
            finally:
                for ref in wanted:
                    if self._asking.get(ref) is done:
                        del self._asking[ref]
                done.set()

        if wanted:
            if spawn is None:
                await run()
                return
            self._asking.update(dict.fromkeys(wanted, done))
            if not spawn(run):
                for ref in wanted:
                    self._asking.pop(ref, None)
                return
            under_way.append(done)
        with anyio.move_on_after(DESCRIBE_SECONDS if wait is None else wait):
            for event in under_way:
                await event.wait()


@dataclass
class Job:
    """An album action running in the background, or its result."""

    album_id: str
    title: str
    action: str  # fill | change | keep | rematch | recheck
    started: float
    done: float | None = None
    ok: bool = False
    message: str = ""
    changed: bool = False  # not done: the match was no longer as the page showed it


# The actions offered for a selection of each list's albums: none needs a choice per album.
BULK: dict[str, tuple[str, ...]] = {
    "review": ("fill", "keep", "rematch"),
    "picked": ("fill", "keep", "rematch"),
    "none": ("recheck",),
}
_DOING = {
    "fill": "Filling",
    "keep": "Keeping",
    "rematch": "Matching again",
    "recheck": "Matching again",
}
BATCH_FAILURES = 50  # albums a batch names with what went wrong (the others are counted)


@dataclass
class Batch:
    """One action for many albums, one album after another in the background. Stopping
    lets the album under way finish and leaves the others as they are: they stay in their
    list, and the action can be started for them again."""

    action: str  # fill | keep | rematch | recheck
    albums: list[str]
    started: float
    name: str = ""  # the list its albums were shown in
    # Each album as the page showed it: acted on only while its match is still so.
    shown: dict[str, Shown] = field(default_factory=dict)
    done: int = 0  # albums the action was tried for
    ok: int = 0
    same: int = 0  # matched again with the result they had
    changed: int = 0  # their match was no longer as the page showed it: left alone
    unchosen: int = 0  # a fill of an album whose match names no most likely release
    failed: list[str] = field(default_factory=list)  # what went wrong, an album each
    failures: int = 0
    current: str = ""  # the album under way
    stopping: bool = False
    finished: float | None = None
    stopped: str = ""  # why it ended before its last album

    @property
    def total(self) -> int:
        return len(self.albums)

    @property
    def doing(self) -> str:
        return _DOING.get(self.action, "Working on")

    def note(self, result: str, message: str = "") -> None:
        """One album's result: ``ok``, ``same``, ``changed``, ``unchosen`` or ``failed``."""
        self.done += 1
        if result == "failed":
            self.failures += 1
            if len(self.failed) < BATCH_FAILURES:
                self.failed.append(message)
        else:
            setattr(self, result, getattr(self, result) + 1)

    def summary(self) -> str:
        """What the action did, once it ended (the failures are listed beside it)."""

        def albums(count: int) -> str:
            return f"{count} album{'s' if count != 1 else ''}"

        if self.action == "fill":
            text = f"{self.ok} of {albums(self.total)} filled."
        elif self.action == "keep":
            as_is = "they are" if self.total != 1 else "it is"
            text = f"{self.ok} of {albums(self.total)} kept as {as_is}."
        else:
            text = f"{albums(self.done)} matched again: {self.ok} now"
            text += f" match{'es' if self.ok == 1 else ''} a release"
            if self.same:
                text += f", {self.same} stay{'s' if self.same == 1 else ''} as before"
            text += "."
        if self.unchosen:
            text += (
                f" {self.unchosen} {'has' if self.unchosen == 1 else 'have'} no most likely"
                " release: choose one for each."
            )
        if self.changed:
            one = self.changed == 1
            text += (
                f" {self.changed} {'has' if one else 'have'} changed since the page was loaded"
                f" and {'was' if one else 'were'} left as {'it is' if one else 'they are'}."
            )
        if self.stopped:
            left = self.total - self.done
            text += f" Stopped before the other {left}: {self.stopped}." if left else ""
        if self.failures:
            text += f" {self.failures} could not be done."
        return text


@dataclass
class Selection:
    """A list as one page showed it to one viewer: what an action for many albums of it
    may act on. Used once."""

    token: str
    viewer: str
    name: str
    albums: dict[str, Shown]
    digest: str
    issued: float
    used: bool = False


# Why a submitted selection starts nothing.
EXPIRED = "expired"
USED = "used"


@dataclass
class Selections:
    """The lists of the pages rendered lately, by token: each kept in memory until half an
    hour after it was issued - whatever is rendered meanwhile -, a bounded number for each
    viewer. A restart forgets them (the page is loaded again)."""

    clock: Callable[[], float] = time.time
    _kept: dict[str, Selection] = field(default_factory=dict)

    def issue(self, viewer: str, name: str, albums: dict[str, Shown]) -> str:
        """The token for this list as it is shown now. The same list shown again to the
        same viewer - a reload, the page's own refresh - is handed the token it has, while
        that was not used and is in the first half of its life: its life is not renewed,
        and a form already on screen stays good until its own half hour is over."""
        now = self.clock()
        self._prune(now)
        text = "\n".join(f"{album} {item.state} {item.release}" for album, item in albums.items())
        digest = hashlib.sha256(text.encode()).hexdigest()
        for kept in self._kept.values():
            if (kept.viewer, kept.name, kept.digest, kept.used) == (
                viewer,
                name,
                digest,
                False,
            ) and now - kept.issued < SELECTION_REUSED_SECONDS:
                return kept.token
        # An album shown as the viewer's last selection of the list showed it shares its
        # entry with that one: kept lists of a large library hold each such album once.
        last = max(
            (s for s in self._kept.values() if (s.viewer, s.name) == (viewer, name)),
            key=lambda s: s.issued,
            default=None,
        )
        if last is not None:
            before = last.albums
            albums = {
                album: same if (same := before.get(album)) == item else item
                for album, item in albums.items()
            }
        token = secrets.token_urlsafe(18)
        self._kept[token] = Selection(token, viewer, name, albums, digest, now)
        mine = sorted(
            (s for s in self._kept.values() if s.viewer == viewer), key=lambda s: s.issued
        )
        for old in mine[: max(0, len(mine) - SELECTIONS_KEPT)]:
            del self._kept[old.token]
        return token

    def find(self, viewer: str, name: str, token: str) -> Selection | str:
        """The viewer's selection of that list, or why it starts nothing: ``EXPIRED`` (not
        known: too old, another viewer's, another list's) or ``USED``."""
        self._prune(self.clock())
        kept = self._kept.get(token)
        if kept is None or (kept.viewer, kept.name) != (viewer, name):
            return EXPIRED
        return USED if kept.used else kept

    def _prune(self, now: float) -> None:
        for token in [t for t, s in self._kept.items() if now - s.issued > SELECTION_SECONDS]:
            del self._kept[token]


@dataclass
class Jobs:
    clock: Callable[[], float] = time.time
    batch: Batch | None = None  # the last action for many albums (running, or its result)
    selections: Selections = field(default_factory=Selections)
    _jobs: dict[str, Job] = field(default_factory=dict)
    _shown: dict[str, set[int]] = field(default_factory=dict)  # viewer -> jobs shown
    _one_fill: anyio.Lock | None = None

    @property
    def one_fill(self) -> anyio.Lock:
        """Fills run one at a time (made in the event loop, on first use)."""
        if self._one_fill is None:
            self._one_fill = anyio.Lock()
        return self._one_fill

    def running(self, album_id: str) -> Job | None:
        job = self._jobs.get(album_id)
        return job if job is not None and job.done is None else None

    def latest(self, album_id: str) -> Job | None:
        """The album's last action: running, or its result while it is remembered."""
        return self._jobs.get(album_id)

    def start(self, album_id: str, title: str, action: str) -> Job:
        job = Job(album_id, title, action, self.clock())
        self._jobs[album_id] = job
        return job

    def claim(self, album_id: str, title: str, action: str) -> Job | None:
        """Start an action unless one is running for the album (no await in between)."""
        if self.running(album_id) is not None:
            return None
        return self.start(album_id, title, action)

    def drop(self, job: Job) -> None:
        """An action answered on the page itself: nothing to show later."""
        if self._jobs.get(job.album_id) is job:
            del self._jobs[job.album_id]

    def finish(self, job: Job, *, ok: bool, message: str) -> None:
        job.done, job.ok, job.message = self.clock(), ok, message

    def begin(self, action: str, selection: Selection, albums: list[str]) -> Batch | None:
        """Start an action for albums of a selection unless one is running (no await in
        between): the selection is used up by it."""
        if self.batch is not None and self.batch.finished is None:
            return None
        selection.used = True
        self.batch = Batch(action, albums, self.clock(), selection.name, selection.albums)
        return self.batch

    def shown_batch(self) -> Batch | None:
        """The action for many albums to show: running, or finished in the last quarter of
        an hour."""
        batch = self.batch
        if batch is None or batch.finished is None:
            return batch
        if self.clock() - batch.finished > JOB_SHOWN_SECONDS:
            self.batch = None
        return self.batch

    def recent(self, viewer: str = "") -> list[Job]:
        """Finished in the last quarter of an hour and not shown to this viewer yet, newest
        first; older ones forgotten."""
        now = self.clock()
        for album_id in [
            a
            for a, j in self._jobs.items()
            if j.done is not None and now - j.done > JOB_SHOWN_SECONDS
        ]:
            del self._jobs[album_id]
        live = {id(j) for j in self._jobs.values()}
        shown = self._shown.setdefault(viewer, set()) & live  # forget what is gone
        done = [j for j in self._jobs.values() if j.done is not None and id(j) not in shown]
        shown.update(id(j) for j in done)
        self._shown[viewer] = shown
        if len(self._shown) > 100:
            self._shown = {viewer: shown}
        return sorted(done, key=lambda j: j.done or 0, reverse=True)


@dataclass
class Settled:
    """What one review-list action did (``Dashboard.settle_review``): the page's notice
    and, for an agent, the same as a state."""

    words: str
    tone: str = "info"  # info | ok | error
    # gone | invalid | refused | busy | resting | kept | started | stopping
    state: str = "invalid"
    job: Job | None = None  # the background action it started


async def library_albums(navidrome: Any, store: Store) -> set[str] | None:
    """The owned albums in the library (IDs): Navidrome's albums less those only the
    catalog made. None when Navidrome cannot be asked."""
    try:
        with anyio.fail_after(10):
            albums = await navidrome.album_counts()
    except Exception:
        return None
    rows = await store.fetchall("SELECT album_id FROM releases WHERE owned_album_id IS NULL")
    catalog_only = {row["album_id"] for row in rows}
    return {album_id for album_id, *_ in albums if album_id not in catalog_only}
