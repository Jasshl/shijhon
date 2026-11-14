"""New releases of the artists an account follows (each person's own choice, off until
they turn it on on the dashboard's New releases page).

**Who is followed.** The artists the account favorited in Navidrome, the artists of the
albums and songs it favorited, and its most played artists: the ``[catalog]
new_releases_most_played`` artists (25) with the most plays among the albums the account
played within the last ``PLAYED_DAYS`` (90) days. Navidrome keeps a play count and the
last play of an album, not each play: an artist's plays are the play counts of those
albums, whenever the plays were. Both are read from Navidrome as the account, with the
sign-in the dashboard saved for it (``getStarred2`` and one page of ``getAlbumList2`` by
last play: two requests a check). "Various Artists" and the like are no artists.

**The check.** Once a day for each account that turned it on (and right after it does), in
the background, one account at a time. For each followed artist it takes the artist's
releases as the artist's page shows them: the saved discography when it was fetched
within the last ``FRESH_SECONDS``, else the catalog's (``CatalogAdditions.discography``:
the artist of that name, among several the one whose releases share the most titles with
the library's), asked at the background pace the library pass shares, as background work,
and saved as that page's discography. That is a search and one list of releases for an
artist (up to three lists when several artists share the name). At most ``[catalog]
new_releases_artists_per_day`` artists are asked about in one check, those not looked at
for the longest first, so that many followed artists are covered over several days. A
catalog that fails or rests ends the check: what was saved stays, and the check is tried
again in an hour.

**What is new.** Of an artist's releases, those the artist's page would add to the library's
(one card per album by the edition rules, none for an album the library has) that were
released within the last ``[catalog] new_releases_days`` (30) days. A date needs a month:
one without a day counts as the first of its month, a year alone is not told and is never
new. Releases dated ahead are kept and show from their day on. A release that is in the
library is kept too: the newest list decides by the account's own selection whether it
stands for the library's copy (``views/newest.py``).

**What is kept.** The followed artists and the releases found, per account, in Shijhon's
database. Nothing is written to the library. Turning it off removes all of it. Log lines
carry counts, never an artist's or an account's name.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import anyio

from shijhon.catalog.base import Catalog, CatalogError, PartialReleases
from shijhon.catalog.base import scope as catalog_scope
from shijhon.catalog.model import CatalogRelease, release_data, release_from_data
from shijhon.matching.normalize import fold
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.store import Store
from shijhon.views.additions import CatalogAdditions, nobody
from shijhon.views.discographies import Discographies

log = logging.getLogger(__name__)

CHECK_SECONDS = 86400.0  # an account's artists are checked this often
RETRY_SECONDS = 3600.0  # after a check Navidrome or the catalog ended
FIRST_SECONDS = 120.0  # after a start: startup work and the first requests go first
LOOK_SECONDS = 600.0  # how often the accounts due are looked for
FRESH_SECONDS = 20 * 3600.0  # a discography saved this recently is not asked for again
PLAYED_DAYS = 90  # albums played within these days count for the most played artists
RECENT_ALBUMS = 500  # albums read of the account's list by last play (one page)
FAILURES_IN_A_ROW = 3  # end a check: Navidrome or the catalog is down
NOT_FOUND = 70  # Navidrome's error code for an item it does not have

# Asks Navidrome as the account: (method, parameters) -> its "subsonic-response".
Read = Callable[[str, Sequence[tuple[str, str]]], Awaitable[dict[str, Any]]]
# An account's saved sign-in, checked now: its reader.
Account = Callable[[str], Awaitable[Read]]


class NotSignedIn(Exception):
    """The account has no saved sign-in that Navidrome accepts for this library."""


class Unavailable(Exception):
    """Navidrome did not answer for the account; nothing is known about its sign-in."""


class _TurnedOff(Exception):
    """The account turned the feature off while it was being checked."""


@dataclass(frozen=True)
class Followed:
    artist_id: str  # Navidrome's
    name: str
    favorite: bool
    played: bool


@dataclass(frozen=True)
class Status:
    enabled: bool
    checked_at: float | None = None  # the last check that finished
    state: str = ""  # why the last one did not: "sign_in", "unavailable"
    artists: int = 0
    checking: bool = False


def released(text: str | None) -> date | None:
    """A catalog date as a day: one without a day is the first of its month; a year
    alone, or anything unreadable, is none."""
    parts = (text or "").split("-")
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:3]):
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 1)
    except ValueError:
        return None


def people(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """The artists (ID, name) of one of Navidrome's album or song entries: each artist it
    lists, else the one it names."""
    listed = entry.get("artists")
    found = [
        (str(a["id"]), str(a["name"]))
        for a in (listed if isinstance(listed, list) else [])
        if isinstance(a, dict) and a.get("id") and a.get("name")
    ]
    if not found and entry.get("artistId") and entry.get("artist"):
        found = [(str(entry["artistId"]), str(entry["artist"]))]
    return found


def _played_at(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class NewReleases:
    def __init__(
        self,
        store: Store,
        navidrome: NavidromeService,
        catalog: Catalog | None,
        additions: CatalogAdditions,
        discographies: Discographies,
        account: Account,
        *,
        days: int = 30,
        most_played: int = 25,
        artists_per_day: int = 150,
        stopping: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.time,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.store = store
        self.navidrome = navidrome
        self.catalog = catalog  # at the background pace
        self.additions = additions
        self.discographies = discographies
        self.account = account
        self._days = days
        self.most_played = most_played
        self.artists_per_day = artists_per_day
        self.stopping = stopping
        self.clock = clock
        self.today = today
        self._wake = anyio.Event()
        # The accounts that turned it on, by their names as apps' requests give them
        # (compared without case, as Navidrome compares them).
        self._accounts: dict[str, str] = {}
        self._checking: set[str] = set()
        # Each account's list as last read, for the day it was read on.
        self._lists: dict[str, tuple[date, tuple[CatalogRelease, ...]]] = {}
        self.checks = 0  # observable in tests: checks that read an account's artists
        # Of an account's saved releases (and its name), those its library does not show
        # it (``views.newest.NewestReleases.missing``); without it, all of them.
        self.missing: (
            Callable[[tuple[CatalogRelease, ...], str], Awaitable[list[CatalogRelease]]] | None
        ) = None

    @property
    def days(self) -> int:
        return self._days

    @days.setter
    def days(self, value: int) -> None:
        """The dashboard's setting, applied at once: the lists are read again."""
        self._days = value
        self._lists.clear()

    # --- who turned it on --------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether accounts can turn it on: a catalog is in use and the window is not 0."""
        return self.catalog is not None and self.days > 0

    async def load(self) -> None:
        """The accounts that turned it on (once, at startup)."""
        rows = await self.store.fetchall("SELECT owner_id, username FROM new_release_accounts")
        self._accounts = {str(r["username"]).casefold(): str(r["owner_id"]) for r in rows}

    def account_of(self, username: str) -> str | None:
        """The account of that name that turned it on (its Navidrome user ID): no work for
        any other. The caller checks that the name is still that account's."""
        if not self._accounts:
            return None
        return self._accounts.get(username.casefold())

    async def turn_on(self, owner: str, username: str) -> None:
        """Turn it on for the account: its first check follows at once."""
        await self.store.execute(
            "INSERT INTO new_release_accounts (owner_id, username, enabled_at) VALUES (?, ?, ?)"
            " ON CONFLICT (owner_id) DO UPDATE SET username = excluded.username",
            [owner, username, self.clock()],
        )
        self._named(owner, username)
        self._wake.set()

    async def turn_off(self, owner: str) -> None:
        """Turn it off: the account's followed artists and releases go."""
        async with self.store.transaction() as db:
            for table in ("new_releases", "new_release_artists", "new_release_accounts"):
                await db.execute(f"DELETE FROM {table} WHERE owner_id = ?", [owner])  # noqa: S608
        self._accounts = {name: o for name, o in self._accounts.items() if o != owner}
        self._lists.pop(owner, None)

    async def seen(self, owner: str, username: str) -> None:
        """The account's current name, as its dashboard session has it (a renamed account
        keeps its list)."""
        if self._accounts.get(username.casefold()) == owner:
            return
        changed = await self.store.execute(
            "UPDATE new_release_accounts SET username = ? WHERE owner_id = ?", [username, owner]
        )
        if changed:
            self._named(owner, username)

    def _named(self, owner: str, username: str) -> None:
        self._accounts = {name: o for name, o in self._accounts.items() if o != owner}
        self._accounts[username.casefold()] = owner

    async def status(self, owner: str) -> Status:
        row = await self.store.fetchone(
            "SELECT a.checked_at, a.state, (SELECT COUNT(*) FROM new_release_artists f"
            " WHERE f.owner_id = a.owner_id) AS artists"
            " FROM new_release_accounts a WHERE a.owner_id = ?",
            [owner],
        )
        if row is None:
            return Status(False)
        return Status(
            True,
            float(row["checked_at"]) if row["checked_at"] is not None else None,
            str(row["state"]),
            int(row["artists"]),
            owner in self._checking,
        )

    async def artists(self, owner: str) -> list[Followed]:
        """The artists the account follows, as its last check read them, by name."""
        rows = await self.store.fetchall(
            "SELECT artist_id, name, favorite, played FROM new_release_artists"
            " WHERE owner_id = ? ORDER BY name COLLATE NOCASE, artist_id",
            [owner],
        )
        return [
            Followed(str(r["artist_id"]), str(r["name"]), bool(r["favorite"]), bool(r["played"]))
            for r in rows
        ]

    # --- the saved list ------------------------------------------------------------------

    async def releases(self, owner: str) -> tuple[CatalogRelease, ...]:
        """The account's saved releases within the window, the newest first, each once:
        what its newest list starts with, before the library's own copies are told. A local
        read; nothing is asked of the catalog or Navidrome."""
        catalog = self.catalog
        if catalog is None or self.days <= 0:
            return ()
        today = self.today()
        known = self._lists.get(owner)
        if known is not None and known[0] == today:
            return known[1]
        rows = await self.store.fetchall(
            "SELECT ref, data FROM new_releases WHERE owner_id = ? AND scope = ?"
            " ORDER BY found_at, artist_id",
            [owner, catalog_scope(catalog)],
        )
        first = today - timedelta(days=self.days)
        found: dict[str, tuple[date, CatalogRelease]] = {}
        for row in rows:
            if str(row["ref"]) in found:
                continue
            try:
                release = release_from_data(json.loads(row["data"]))
            except Exception as exc:  # written by another version, or damaged
                log.info("a saved new release unreadable: %s", type(exc).__name__)
                continue
            day = released(release.release_date)
            if day is not None and first <= day <= today:
                found[str(row["ref"])] = (day, release)
        ordered = sorted(found.values(), key=lambda f: (-f[0].toordinal(), fold(f[1].title)))
        releases = tuple(release for _, release in ordered)
        if len(self._lists) > 1024:
            self._lists.clear()
        self._lists[owner] = (today, releases)
        return releases

    async def listed(self, owner: str, username: str) -> list[CatalogRelease]:
        """The account's new releases as its apps' newest list, the dashboard and the
        agent tool show them: the saved ones its library does not show it. ``username``:
        the account's name as Navidrome just gave it."""
        saved = await self.releases(owner)
        if not saved or self.missing is None:
            return list(saved)
        return await self.missing(saved, username)

    # --- the check -----------------------------------------------------------------------

    async def run(self) -> None:
        """The background work: each account's check when it is due."""
        with anyio.move_on_after(FIRST_SECONDS):
            await self._wake.wait()
        while not self.stopping():
            self._wake = anyio.Event()
            if self.available:
                try:
                    await self.check_due()
                except Exception as exc:
                    log.warning("new releases: the check failed: %s", type(exc).__name__)
            with anyio.move_on_after(LOOK_SECONDS):
                await self._wake.wait()

    async def check_due(self) -> int:
        """Check every account that is due, one at a time; how many were."""
        rows = await self.store.fetchall(
            "SELECT owner_id FROM new_release_accounts WHERE next_at <= ? ORDER BY next_at",
            [self.clock()],
        )
        for row in rows:
            if self.stopping():
                break
            owner = str(row["owner_id"])
            try:
                await self.check(owner)
            except Exception as exc:  # this account later; the others are still checked
                log.warning("new releases: an account's check failed: %s", type(exc).__name__)
                await self._ended(owner, "unavailable", self.clock() + RETRY_SECONDS)
        return len(rows)

    async def check(self, owner: str) -> None:
        """One account's check: its followed artists read again, the catalog asked about
        those due, what is new saved."""
        catalog = self.catalog
        if catalog is None or self.days <= 0:
            return
        self._checking.add(owner)
        try:
            await self._check(owner, catalog)
        except _TurnedOff:
            pass
        finally:
            self._checking.discard(owner)
            self._lists.pop(owner, None)

    async def _check(self, owner: str, catalog: Catalog) -> None:
        started = self.clock()
        try:
            read = await self.account(owner)
            followed = await self._followed(read)
        except NotSignedIn:
            log.info("new releases: an account's saved sign-in was not accepted; not checked")
            await self._ended(owner, "sign_in", started + CHECK_SECONDS)
            return
        except Exception as exc:  # Navidrome down, or an answer that cannot be read
            log.info("new releases: an account's artists not read: %s", type(exc).__name__)
            await self._ended(owner, "unavailable", started + RETRY_SECONDS)
            return
        self.checks += 1
        await self._keep_artists(owner, followed)
        rows = await self.store.fetchall(
            "SELECT artist_id, name FROM new_release_artists WHERE owner_id = ?"
            " ORDER BY checked_at IS NOT NULL, checked_at, name, artist_id",
            [owner],
        )
        scope = catalog_scope(catalog)
        asked = looked = failures = 0
        state = ""
        for row in rows:
            if self.stopping():
                return  # the check goes on after the restart
            if asked >= self.artists_per_day:
                break  # the others at the next check, those waiting longest first
            try:
                asked += await self._artist(owner, str(row["artist_id"]), str(row["name"]), catalog)
                looked += 1
                failures = 0
            except CatalogError as exc:
                log.info("new releases: the catalog did not answer: %s", exc.reason)
                if exc.kind in ("unavailable", "rate_limited", "unauthorized"):
                    state = "unavailable"  # it fails or rests: what was saved stays
                    break
                failures += 1
            except _TurnedOff:
                raise
            except Exception as exc:
                log.info("new releases: an artist not checked: %s", type(exc).__name__)
                failures += 1
            if failures >= FAILURES_IN_A_ROW:
                state = "unavailable"
                break
        first = (self.today() - timedelta(days=self.days)).isoformat()
        async with self.store.transaction() as db:
            # What left the window, and what another catalog or region found.
            await db.execute(
                "DELETE FROM new_releases WHERE owner_id = ? AND (scope <> ? OR"
                " (length(release_date) >= 10 AND release_date < ?))",
                [owner, scope, first],
            )
        saved = await self.store.fetchone(
            "SELECT COUNT(DISTINCT ref) AS n FROM new_releases WHERE owner_id = ?", [owner]
        )
        log.info(
            "new releases: an account's check looked at %d of %d followed artists"
            " (%d asked of the catalog)%s; %d releases saved",
            looked,
            len(rows),
            asked,
            "; ended early, tried again later" if state else "",
            int(saved["n"]) if saved is not None else 0,
        )
        await self._ended(
            owner, state, started + (RETRY_SECONDS if state else CHECK_SECONDS), finished=not state
        )

    async def _ended(
        self, owner: str, state: str, next_at: float, *, finished: bool = False
    ) -> None:
        await self.store.execute(
            "UPDATE new_release_accounts SET state = ?, next_at = ?,"
            " checked_at = CASE WHEN ? THEN ? ELSE checked_at END WHERE owner_id = ?",
            [state, next_at, int(finished), self.clock(), owner],
        )

    async def _followed(self, read: Read) -> dict[str, Followed]:
        """The artists the account follows now, read from Navidrome as the account."""
        library = str(self.navidrome.library_id)
        found: dict[str, Followed] = {}

        def follow(ident: str, name: str, *, favorite: bool = False, played: bool = False) -> None:
            if nobody(name):
                return
            known = found.get(ident)
            found[ident] = Followed(
                ident,
                name,
                favorite or (known is not None and known.favorite),
                played or (known is not None and known.played),
            )

        starred = (await read("getStarred2", [("musicFolderId", library)])).get("starred2")
        starred = starred if isinstance(starred, dict) else {}
        for artist in _dicts(starred.get("artist")):
            if artist.get("id") and artist.get("name"):
                follow(str(artist["id"]), str(artist["name"]), favorite=True)
        for kind in ("album", "song"):
            for entry in _dicts(starred.get(kind)):
                for ident, name in people(entry):
                    follow(ident, name, favorite=True)
        if self.most_played > 0:
            answer = await read(
                "getAlbumList2",
                [("type", "recent"), ("size", str(RECENT_ALBUMS)), ("musicFolderId", library)],
            )
            listed = answer.get("albumList2")
            since = self.clock() - PLAYED_DAYS * 86400
            plays: Counter[str] = Counter()
            names: dict[str, str] = {}
            for album in _dicts(listed.get("album") if isinstance(listed, dict) else None):
                at = _played_at(album.get("played"))
                count = album.get("playCount")
                if at is None or at < since or not isinstance(count, int) or count <= 0:
                    continue
                for ident, name in people(album):
                    if not nobody(name):
                        plays[ident] += count
                        names[ident] = name
            most = sorted(plays, key=lambda i: (-plays[i], fold(names[i]), i))
            for ident in most[: self.most_played]:
                follow(ident, names[ident], played=True)
        return found

    async def _keep_artists(self, owner: str, followed: dict[str, Followed]) -> None:
        """The account's followed artists as just read: those no longer followed go, with
        their releases; the others keep when they were last looked at."""
        async with self.store.transaction() as db:
            await _still_on(db, owner)
            cursor = await db.execute(
                "SELECT artist_id FROM new_release_artists WHERE owner_id = ?", [owner]
            )
            gone = [str(r[0]) for r in await cursor.fetchall() if str(r[0]) not in followed]
            for table in ("new_releases", "new_release_artists"):
                await db.executemany(
                    f"DELETE FROM {table} WHERE owner_id = ? AND artist_id = ?",  # noqa: S608
                    [(owner, ident) for ident in gone],
                )
            await db.executemany(
                "INSERT INTO new_release_artists (owner_id, artist_id, name, favorite, played)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (owner_id, artist_id) DO UPDATE SET"
                " name = excluded.name, favorite = excluded.favorite, played = excluded.played",
                [
                    (owner, f.artist_id, f.name, int(f.favorite), int(f.played))
                    for f in followed.values()
                ],
            )

    async def _artist(self, owner: str, artist_id: str, name: str, catalog: Catalog) -> int:
        """One followed artist: its releases that are new, saved for the account. 1 when
        the catalog was asked, 0 when the saved discography was recent enough."""
        try:
            answer = await self.navidrome.subsonic("getArtist", [("id", artist_id)])
        except NavidromeError as exc:
            if exc.code != NOT_FOUND:
                raise
            await self._found(owner, artist_id, catalog, ())  # an artist the library lost
            return 0
        entry = answer.get("artist")
        owned = _dicts(entry.get("album") if isinstance(entry, dict) else None)
        key = f"{catalog_scope(catalog)}:name:{fold(name)}"
        fetched = await self.discographies.fetched_at(key)
        saved = None
        if fetched is not None and self.clock() - fetched < FRESH_SECONDS:
            saved = await self.discographies.get(key)
        asked = 0
        if saved is not None:
            releases: tuple[CatalogRelease, ...] = saved.releases
        else:
            asked = 1
            releases = await self.additions.discography(catalog, name, owned)
            if isinstance(releases, PartialReleases):
                return asked  # a part of the list: looked at again at the next check
            await self.discographies.save(key, tuple(releases))
        first = self.today() - timedelta(days=self.days)

        def recent(release: CatalogRelease) -> bool:
            day = released(release.release_date)
            return day is not None and day >= first

        # As the artist's page adds them; and those of them the library has, which the
        # newest list tells from the account's own selection.
        missing = [r for r in await self.additions.not_in_library(releases, owned) if recent(r)]
        candidates = [r for r in releases if recent(r)]
        held = await self.additions.in_library([str(r.ref) for r in candidates])
        missing += [r for r in candidates if str(r.ref) in held]
        await self._found(owner, artist_id, catalog, missing)
        return asked

    async def _found(
        self, owner: str, artist_id: str, catalog: Catalog, releases: Sequence[CatalogRelease]
    ) -> None:
        """What a followed artist's check found replaces what it found before."""
        now = self.clock()
        scope = catalog_scope(catalog)
        async with self.store.transaction() as db:
            await _still_on(db, owner)
            cursor = await db.execute(
                "SELECT ref, found_at FROM new_releases WHERE owner_id = ? AND artist_id = ?",
                [owner, artist_id],
            )
            before = {str(r[0]): float(r[1]) for r in await cursor.fetchall()}
            await db.execute(
                "DELETE FROM new_releases WHERE owner_id = ? AND artist_id = ?", [owner, artist_id]
            )
            await db.executemany(
                "INSERT OR REPLACE INTO new_releases"
                " (owner_id, artist_id, ref, scope, release_date, data, found_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        owner,
                        artist_id,
                        str(r.ref),
                        scope,
                        r.release_date or "",
                        json.dumps(release_data(r)),
                        before.get(str(r.ref), now),
                    )
                    for r in releases
                ],
            )
            await db.execute(
                "UPDATE new_release_artists SET checked_at = ?"
                " WHERE owner_id = ? AND artist_id = ?",
                [now, owner, artist_id],
            )


async def _still_on(db: Any, owner: str) -> None:
    cursor = await db.execute("SELECT 1 FROM new_release_accounts WHERE owner_id = ?", [owner])
    if await cursor.fetchone() is None:
        raise _TurnedOff


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []
