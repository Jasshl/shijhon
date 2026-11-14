"""Catalog albums and an artist's releases added to an account's library for an agent.

What an agent adds is added as the account's own action is: each release goes through the
commits (``views/commits.py``: into the owned album it completes, else as a catalog album,
with their refusals and, for the account, the daily cap on adding), and once Navidrome
shows the account the album it is recorded as the account's activity
(``CollectionRequests.added``), so it joins the account's Shijhon library like a catalog
album the account rated or put into a playlist.

A request is accepted once under the agent's idempotency key and kept
(``library_additions``); a worker adds its releases one after another. Before each
release, and at the engine's write gates, the agent's grant is checked again: revoked, or
without the permission, the request stops and what is left is not added. A restart goes
on where it stopped: adding a release that is in the library writes nothing.

Bounds: a request names at most ``MAX_RELEASES`` releases, an account has at most
``MAX_OPEN`` unfinished requests, and an artist stands for the releases the artist's page
in apps shows (one edition of each album, by the edition rules), of the asked types, the
newest first, never more than the request allows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

import anyio
from mcp.server import MCPServer
from mcp.types import CallToolResult, ToolAnnotations

from shijhon.access import Principal
from shijhon.catalog.base import Catalog, CatalogError, PartialReleases
from shijhon.catalog.editions import dedupe_editions
from shijhon.catalog.model import CatalogRelease, ReleaseKind, Twins, release_from_data
from shijhon.matching.normalize import fold
from shijhon.placeholders.daily_cap import adding_for
from shijhon.playlists.model import Connection, PlaylistError, UpstreamUnavailable
from shijhon.playlists.requests import RequestError, SetupRequired
from shijhon.views.commits import CommitError, Commits
from shijhon.views.entries import album_name
from shijhon.views.ids import CatalogId, album_id, artist_id
from shijhon.views.virtual import NOT_FOUND

if TYPE_CHECKING:
    from shijhon.agents.tools import Tools
    from shijhon.playlists.service import Playlists
    from shijhon.views.additions import CatalogAdditions
    from shijhon.views.collection_requests import CollectionRequests

log = logging.getLogger(__name__)

MAX_RELEASES = 25  # releases one request names at most
ARTIST_RELEASES = 10  # ... and an artist stands for, unless the request says otherwise
MAX_OPEN = 3  # an account's unfinished requests
KEPT = 100  # an account's finished requests kept
SEARCH_RESULTS = 25  # albums and artists a catalog search returns at most
ADD_SECONDS = 180.0  # one release: its catalog answer, its files and Navidrome's scan
ANSWER_WAIT = 8.0  # the longest an accepted request's answer waits for the worker
ANSWER_MARGIN = 6.0  # what the call's deadline keeps for the rest of its work
ANSWER_POLL = 0.2
WORK_SECONDS = 60.0  # the worker looks for work this often without being told
RETRY_SECONDS = 30.0  # ... and this long after Navidrome did not answer
DESCRIBE_SECONDS = 8.0  # a preview's wait for the catalog to describe named albums
UNFINISHED = ("queued", "running")
CATALOG_SETUP = "A Navidrome administrator chooses a catalog on the Catalog page."
Kind = Literal["album", "ep", "single", "compilation"]
KINDS: tuple[Kind, ...] = ("album", "ep", "single", "compilation")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{8,128}")

# Why a release was not added, as the request's status says it.
REASONS = {
    "not_found": "The catalog has no such album.",
    "catalog_unavailable": "The catalog did not answer. Ask for it again later, in a new request.",
    "album_collision": "It conflicts with an album the library already has. An "
    "administrator can look at that album on the Library page.",
    "owned_fill": "It completes an album in the library whose missing songs could not be "
    "added now.",
    "selection_changed": "The catalog's release changed while it was added. Ask for it "
    "again in a new request.",
    "library_preparation": "It could not be prepared in the library.",
    "not_shown": "It is in the server's library, but Navidrome does not show it to this "
    "account, so it was not added to the account's library.",
    "took_too_long": "Adding it took too long. Ask for it again later, in a new request.",
    "stopped": "The request stopped before this release.",
    "stopped_written": "The request stopped after this release was written to the server's "
    "library and before it joined this account's library. It is not in the account's "
    "library; the cleanup takes it out again if nobody uses it.",
}
# The worker's boundaries (``LibraryAdditions.step``), in a release's order.
STEPS = ("before commit", "committed", "recorded", "saved", "ending")


class Stopped(Exception):
    """The agent's grant no longer allows the request: nothing more is added."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def release_item(release: CatalogRelease) -> dict[str, Any]:
    """A catalog release as a search, a preview and a request list it."""
    return {
        "album_id": album_id(release.ref),
        "title": album_name(release),
        "artist": release.artist,
        "type": release.kind.value,
        "release_date": release.release_date or "",
        "tracks": release.track_count or len(release.tracks),
    }


class LibraryAdditions:
    def __init__(
        self,
        service: Playlists,
        additions: CatalogAdditions | None,
        recorder: CollectionRequests | None,
    ) -> None:
        self.service, self.store = service, service.repo.store
        self.additions = additions  # the artist page's rules: what the library lacks
        self.recorder = recorder  # records an added album as the account's activity
        self.wake = anyio.Event()
        self.added = 0  # releases added by requests since the start (observable in tests)
        # Test hook: called with a boundary's name (``STEPS``) between the worker's steps.
        self.step: Callable[[str], Awaitable[None]] | None = None

    async def _at(self, step: str) -> None:
        if self.step is not None:
            await self.step(step)

    # --- what can be added -------------------------------------------------------------

    def catalog(self) -> tuple[Catalog, Commits]:
        """The installation's catalog and its commits, or what is missing."""
        catalog, commits = self.service.catalog, self.service.commits
        if catalog is None or commits is None or self.recorder is None:
            raise SetupRequired(
                "catalog", "Adding to the library needs a catalog. " + CATALOG_SETUP
            )
        return catalog, commits

    def album(self, text: str) -> CatalogId:
        """A catalog album ID of this installation's catalog, as apps and the agent tools
        show them (``sh.al.<catalog>.<id>``)."""
        catalog, _ = self.catalog()
        cid = CatalogId.parse(text) if isinstance(text, str) and len(text) <= 300 else None
        if cid is None or cid.kind != "al" or cid.ref.catalog != catalog.key:
            raise RequestError(
                "Use catalog album IDs as search_catalog and get_new_releases return them "
                "(album_id)."
            )
        return cid

    def artist(self, text: str) -> CatalogId:
        catalog, _ = self.catalog()
        cid = CatalogId.parse(text) if isinstance(text, str) and len(text) <= 300 else None
        if cid is None or cid.kind != "ar" or cid.ref.catalog != catalog.key:
            raise RequestError("Use a catalog artist ID as search_catalog returns it (artist_id).")
        return cid

    @staticmethod
    def unavailable(exc: CatalogError) -> PlaylistError:
        """A catalog failure as a tool answers it (its reason stays in the log)."""
        log.info("agent catalog request failed: %s", exc.kind)
        if exc.kind == "not_found":
            return RequestError("The catalog has no such item.", 404)
        return UpstreamUnavailable("The catalog did not answer. Try again later.")

    async def search(self, query: str, limit: int) -> dict[str, Any]:
        """Catalog albums (one edition of each, by the edition rules) and artists for a
        term, in the catalog's order."""
        catalog, _ = self.catalog()
        term = " ".join(query.split())
        if not term or len(term) > 200 or not 1 <= limit <= SEARCH_RESULTS:
            raise RequestError(
                f"Search with 1 to 200 characters and ask for 1 to {SEARCH_RESULTS} results."
            )
        try:
            found = await catalog.search(term, limit)
        except CatalogError as exc:
            raise self.unavailable(exc) from None
        twins = self.additions.twins if self.additions is not None else Twins.EXPLICIT
        albums = dedupe_editions(found.albums, twins=twins)
        return {
            "albums": [release_item(release) for release in albums[:limit]],
            "artists": [
                {"artist_id": artist_id(artist.ref), "name": artist.name}
                for artist in found.artists[:limit]
            ],
        }

    async def _owned(self, connection: Connection, name: str) -> list[dict[str, Any]]:
        """The library's albums of the artist with this name, as Navidrome shows them to
        the account (none: the library has no such artist)."""
        assert self.additions is not None
        native = (await self.additions.library_ids()).get(fold(name))
        if native is None:
            return []
        reply = await self.service.client.call(connection, "getArtist", [("id", native)])
        albums = reply.get("artist", {}).get("album", [])
        return [album for album in albums if isinstance(album, dict)]

    async def of_artist(
        self, connection: Connection, cid: CatalogId, kinds: Sequence[str], most: int
    ) -> tuple[str, list[CatalogRelease], int]:
        """(The artist's name, the releases an addition of the artist stands for, how
        many there are of the asked types): what the artist's page in apps shows of the
        catalog - one edition of each album the library lacks - and the artist's releases
        the library has, which then join the account's library too; of ``kinds``, the
        newest first, at most ``most``."""
        catalog, _ = self.catalog()
        if self.additions is None:
            raise SetupRequired(
                "catalog", "Adding to the library needs a catalog. " + CATALOG_SETUP
            )
        try:
            artist = await catalog.artist(cid.ref.id)
            releases = await catalog.artist_releases(cid.ref.id)
        except CatalogError as exc:
            raise self.unavailable(exc) from None
        if isinstance(releases, PartialReleases):
            # A part of the list: what it lacks could be an album the library has.
            raise UpstreamUnavailable(
                "The catalog's list of this artist's releases could not be read in full. "
                "Try again later."
            )
        owned = await self._owned(connection, artist.name)
        cards = await self.additions.not_in_library(releases, owned)
        held = await self.additions.in_library([str(r.ref) for r in releases])
        shown = {str(r.ref) for r in cards}
        cards += [r for r in releases if str(r.ref) in held and str(r.ref) not in shown]
        wanted = {ReleaseKind(kind) for kind in kinds}
        # The newest first; releases without a date after the dated ones.
        listed = sorted(
            (r for r in cards if r.kind in wanted), key=lambda r: r.release_date or "", reverse=True
        )
        return artist.name, listed[:most], len(listed)

    async def described(self, cids: Sequence[CatalogId]) -> list[dict[str, Any]]:
        """Named albums as the catalog describes them, for a preview: within a few seconds,
        a few at a time. One the catalog does not have says so; one not described in time
        is listed by its ID alone."""
        catalog, _ = self.catalog()
        found: dict[str, dict[str, Any]] = {}
        limiter = anyio.CapacityLimiter(4)

        async def one(cid: CatalogId) -> None:
            async with limiter:
                try:
                    found[str(cid)] = release_item(await catalog.album(cid.ref.id))
                except CatalogError as exc:
                    if exc.kind == "not_found":
                        found[str(cid)] = {
                            "album_id": str(cid),
                            "state": "not_found",
                            "detail": REASONS["not_found"],
                        }

        with anyio.move_on_after(DESCRIBE_SECONDS):
            async with anyio.create_task_group() as group:
                for cid in cids:
                    group.start_soon(one, cid)
        return [found.get(str(cid), {"album_id": str(cid)}) for cid in cids]

    # --- requests ----------------------------------------------------------------------

    @staticmethod
    def canonical(albums: Sequence[str], artist: str, kinds: Sequence[str], most: int) -> str:
        """What a request asks for: the same key with other input is a conflict."""
        asked = {"albums": list(albums), "artist": artist, "kinds": sorted(kinds), "most": most}
        return json.dumps(asked, sort_keys=True, separators=(",", ":"))

    async def replay(self, owner: str, creator: str, key: str, canonical: str) -> str | None:
        """The request accepted before under this key, if any (the same agent, the same
        input: else a refusal)."""
        if not _KEY.fullmatch(key):
            raise RequestError("Send an idempotency_key of 8 to 128 plain characters.")
        old = await self.store.fetchone(
            "SELECT id,creator,library,input_hash FROM library_additions"
            " WHERE owner=? AND request_key=?",
            [owner, key],
        )
        if old is None:
            return None
        if old["creator"] != creator or old["library"] != self.service.requests.library:
            raise RequestError("This request key is unavailable for this agent.", 403)
        if old["input_hash"] != hashlib.sha256(canonical.encode()).hexdigest():
            raise RequestError("This request key was already used with different input.", 409)
        return str(old["id"])

    async def accept(
        self,
        owner: str,
        creator: str,
        key: str,
        canonical: str,
        items: list[dict[str, Any]],
    ) -> tuple[str, bool]:
        """Keep a request for the worker: (its ID, whether it is new). The caller holds the
        service's changes, as for a playlist request: a revocation is ordered with it."""
        replayed = await self.replay(owner, creator, key, canonical)
        if replayed is not None:
            return replayed, False
        await self.service.requests.grant(creator, "add")
        if not 1 <= len(items) <= MAX_RELEASES:
            raise RequestError(f"Add 1 to {MAX_RELEASES} releases in one request.")
        ident, now = secrets.token_hex(12), time.time()
        async with self.store.transaction() as db:
            cursor = await db.execute(
                "SELECT count(*) AS n FROM library_additions WHERE owner=?"
                " AND state IN ('queued','running')",
                [owner],
            )
            row = await cursor.fetchone()
            if row is not None and row["n"] >= MAX_OPEN:
                raise RequestError(
                    "Earlier additions for this account are still being added. Read them "
                    "with get_library_addition and wait for them to finish.",
                    409,
                )
            await db.execute(
                "INSERT INTO library_additions"
                "(id,owner,creator,library,request_key,input_hash,items,created,updated)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ident,
                    owner,
                    creator,
                    self.service.requests.library,
                    key,
                    hashlib.sha256(canonical.encode()).hexdigest(),
                    json.dumps(items),
                    now,
                    now,
                ],
            )
            # Finished requests are kept for their status, the newest of them.
            await db.execute(
                "DELETE FROM library_additions WHERE owner=? AND state NOT IN"
                " ('queued','running') AND id NOT IN (SELECT id FROM library_additions"
                " WHERE owner=? ORDER BY created DESC LIMIT ?)",
                [owner, owner, KEPT],
            )
        self.notify()
        return ident, True

    async def status(self, owner: str, creator: str, ident: str) -> dict[str, Any]:
        """One of this agent connection's requests, with each release's outcome."""
        row = (
            await self.store.fetchone(
                "SELECT * FROM library_additions WHERE id=? AND owner=? AND creator=?",
                [ident, owner, creator],
            )
            if isinstance(ident, str) and 0 < len(ident) <= 100
            else None
        )
        if row is None:
            raise RequestError("This connection has no such addition.", 404)
        items = json.loads(row["items"])
        releases = []
        for item in items:
            shown = {k: v for k, v in item.items() if k not in ("message", "writing")}
            reason = item.get("reason", "")
            if reason:
                shown["detail"] = item.get("message") or REASONS.get(reason, "")
            releases.append(shown)
        counts = {
            state: sum(1 for item in items if item["state"] == state)
            for state in ("added", "already_in_library", "pending")
        }
        not_added = len(items) - sum(counts.values())
        limited = sum(1 for item in items if item.get("reason") == "daily_limit")
        answer: dict[str, Any] = {
            "id": row["id"],
            "state": row["state"],
            "releases": releases,
            "added": counts["added"],
            "already_in_library": counts["already_in_library"],
            "not_added": not_added,
            "pending": counts["pending"],
        }
        if row["reason"]:
            answer["stopped_because"] = row["reason"]
        if limited:
            said = next(
                (item.get("message") for item in items if item.get("reason") == "daily_limit"), ""
            )
            answer["daily_limit"] = {
                "reached": True,
                "left_out": limited,
                "message": f"{said} {counts['added']} of {len(items)} releases were added; "
                f"{limited} were left out for the limit and can be asked for again later, "
                "in a new request.",
            }
        if row["state"] in UNFINISHED:
            answer["next"] = (
                f"The releases are being added. Call get_library_addition with request_id "
                f"{row['id']} for the result; do not send the request again."
            )
        return answer

    async def settled(self, owner: str, creator: str, ident: str) -> dict[str, Any]:
        """An accepted request's status once the worker is done with it, or as far as it
        got within the call's time."""
        left = anyio.current_effective_deadline() - anyio.current_time() - ANSWER_MARGIN
        with anyio.move_on_after(max(0.0, min(ANSWER_WAIT, left))):
            while True:
                if (await self.status(owner, creator, ident))["state"] not in UNFINISHED:
                    break
                await anyio.sleep(ANSWER_POLL)
        return await self.status(owner, creator, ident)

    # --- the worker --------------------------------------------------------------------

    def notify(self) -> None:
        self.wake.set()

    async def run(self) -> None:
        """Add the accepted requests' releases, one request and one release at a time; at
        once when told of a new request, else every minute (a restart's leftovers)."""
        while True:
            self.wake = anyio.Event()
            wait = WORK_SECONDS
            try:
                wait = await self.tick()
            except Exception as exc:
                log.warning("library additions failed: %s", type(exc).__name__)
            with anyio.move_on_after(wait):
                await self.wake.wait()

    async def tick(self) -> float:
        """Work on the unfinished requests, the oldest first. Returns how long to wait
        before looking again when nothing tells of new work."""
        while True:
            row = await self.store.fetchone(
                "SELECT * FROM library_additions WHERE state IN ('queued','running')"
                " AND library=? ORDER BY created,id LIMIT 1",
                [self.service.requests.library],
            )
            if row is None:
                return WORK_SECONDS
            try:
                await self._work(dict(row))
            except UpstreamUnavailable:
                return RETRY_SECONDS  # Navidrome is away: the request waits as it is

    async def _authorized(self, request: dict[str, Any]) -> tuple[Connection, Principal]:
        """The account's connection and its current identity while the agent that asked
        may still add to its library; else ``Stopped``. Navidrome not answering is no
        answer (``UpstreamUnavailable``)."""
        try:
            connection, _ = await self.service.requests.grant(request["creator"], "add")
            if connection.user_id != request["owner"]:
                raise RequestError("This request belongs to another owner.", 403)
            principal = await self.service.client.principal(connection)
        except UpstreamUnavailable:
            raise
        except RequestError as exc:
            if exc.status == 403:
                raise Stopped(
                    "The agent's connection no longer has the permission to add to the library."
                ) from None
            raise Stopped("The agent that asked was disconnected.") from None
        except PlaylistError:
            raise Stopped("The account's connection to Navidrome is no longer valid.") from None
        if principal.user_id != request["owner"]:
            raise Stopped("The account's connection to Navidrome is no longer valid.")
        return connection, principal

    async def _save(
        self, request: dict[str, Any], items: list[dict[str, Any]], state: str, reason: str = ""
    ) -> None:
        await self.store.execute(
            "UPDATE library_additions SET items=?,state=?,reason=?,updated=? WHERE id=?",
            [json.dumps(items), state, reason, time.time(), request["id"]],
        )

    async def _work(self, request: dict[str, Any]) -> None:
        items: list[dict[str, Any]] = json.loads(request["items"])
        for item in items:
            if item["state"] != "pending":
                continue
            try:
                connection, principal = await self._authorized(request)
                await self._add(request, connection, principal, items, item)
            except UpstreamUnavailable:
                raise  # Navidrome is away: the release stays to do
            except Stopped as stopped:
                for left in items:
                    if left["state"] == "pending":
                        # One this request wrote before it stopped (a restart in between)
                        # is in the server's library, not in the account's: said so.
                        written = left.pop("writing", False) and await self._in_library(left)
                        reason = "stopped_written" if written else "stopped"
                        left.update(state="not_added", reason=reason)
                await self._save(request, items, "stopped", stopped.reason)
                log.info("a library addition stopped: its agent may no longer add")
                return
            except Exception as exc:  # this release alone: the others are still added
                log.warning("a library addition's release failed: %s", type(exc).__name__)
                item.pop("writing", None)
                item.update(state="not_added", reason="library_preparation")
            await self._save(request, items, "running")
            await self._at("saved")
        await self._at("ending")
        await self._save(request, items, "finished")

    async def _in_library(self, item: dict[str, Any]) -> bool:
        cid = CatalogId.parse(item["album_id"])
        _, commits = self.catalog()
        return cid is not None and await commits.materialized.album(cid.ref) is not None

    async def _add(
        self,
        request: dict[str, Any],
        connection: Connection,
        principal: Principal,
        items: list[dict[str, Any]],
        item: dict[str, Any],
    ) -> None:
        """Add one release (``item``, of the request's ``items``) for the account and say
        on it how it went. ``Stopped`` (from a write gate too) and Navidrome not answering
        end the request's turn with the release still to do."""
        _, commits = self.catalog()
        assert self.recorder is not None
        cid = CatalogId.parse(item["album_id"])
        assert cid is not None

        async def authorize() -> None:
            await self._authorized(request)

        def refuse(reason: str, message: str = "") -> None:
            item.pop("writing", None)
            item.update(state="not_added", reason=reason)
            if message:
                item["message"] = message

        try:
            before = await commits.materialized.album(cid.ref)
            # Whether this request writes the release is kept before it does: a restart
            # between the write and the request's record still reports it as added. (A
            # release someone else added in exactly that gap would be reported so too.)
            new = before is None or bool(item.get("writing"))
            if before is None and not item.get("writing"):
                item["writing"] = True
                await self._save(request, items, "running")
            await self._at("before commit")
            # What this adds is added for the account (the daily cap on adding), and the
            # agent's grant is asked again where files are written.
            with (
                anyio.fail_after(ADD_SECONDS),
                commits.engine.guard_writes(authorize),
                adding_for(request["owner"]),
            ):
                native = await commits.native(cid, "agent")
            await self._at("committed")
        except CommitError as exc:
            if exc.category == "daily_limit":
                # In the commit's own words: they say what the limit is about.
                refuse("daily_limit", exc.message)
            elif exc.code == NOT_FOUND:
                refuse("not_found")
            elif exc.message == "catalog unavailable":
                refuse("catalog_unavailable")
            elif exc.category in REASONS:
                refuse(exc.category)
            else:
                refuse("library_preparation")
            return
        except TimeoutError:
            refuse("took_too_long")
            return
        await self._describe(item, cid)
        try:
            # Navidrome shows the account the album: only then is it the account's.
            reply = await self.service.client.call(connection, "getAlbum", [("id", native)])
        except UpstreamUnavailable:
            raise
        except PlaylistError:
            reply = {}
        if reply.get("album", {}).get("id") != native:
            refuse("not_shown")
            return
        await self._authorized(request)
        await self.recorder.added(principal, [str(cid)])
        await self._at("recorded")
        item.pop("writing", None)
        item["state"] = "added" if new else "already_in_library"
        if new:
            self.added += 1

    async def _describe(self, item: dict[str, Any], cid: CatalogId) -> None:
        """The release as the library holds it now, for a request that named only its ID."""
        if item.get("title"):
            return
        row = await self.store.fetchone("SELECT data FROM releases WHERE ref=?", [str(cid.ref)])
        if row is not None:
            try:
                item.update(release_item(release_from_data(json.loads(row["data"]))))
            except (ValueError, KeyError, TypeError):
                return


class LibraryTools:
    """The adding tools, for the account of the agent's sign-in."""

    def __init__(self, tools: Tools, adder: LibraryAdditions | None) -> None:
        self.tools, self.adder = tools, adder

    def service(self) -> LibraryAdditions:
        if self.adder is None:
            raise SetupRequired(
                "catalog", "Adding to the library needs a catalog. " + CATALOG_SETUP
            )
        self.adder.catalog()
        return self.adder

    async def search(self, query: str, limit: int) -> dict[str, Any]:
        await self.tools.auth("add")
        adder = self.service()
        found = await adder.search(query, limit)
        await self.tools.auth("add")
        return found

    async def add(
        self,
        albums: list[str],
        artist: str,
        kinds: list[str],
        most: int | None,
        preview: bool,
        key: str,
    ) -> dict[str, Any]:
        tools = self.tools
        token = await tools.auth("add")
        owner, creator = str(token.subject), tools.grant(token)
        adder = self.service()
        connection = await tools.service.connection(owner)
        if bool(albums) == bool(artist):
            raise RequestError("Name album_ids, or one artist_id: one of the two.")
        if len(albums) > MAX_RELEASES or len(set(albums)) != len(albums):
            raise RequestError(f"Name up to {MAX_RELEASES} different albums in one request.")
        if not set(kinds) <= set(KINDS) or len(set(kinds)) != len(kinds):
            raise RequestError("release_types are album, ep, single and compilation.")
        if albums and (kinds or most is not None):
            raise RequestError("release_types and max_releases belong to an artist_id.")
        kinds = kinds or ["album"]
        most = ARTIST_RELEASES if most is None else most
        if not 1 <= most <= MAX_RELEASES:
            raise RequestError(f"max_releases is 1 to {MAX_RELEASES}.")
        canonical = adder.canonical(albums, artist, kinds, most)
        if not preview:
            # A request accepted before is answered as it stands: the catalog is not
            # asked again, and an artist's list is the one accepted then.
            replayed = await adder.replay(owner, creator, key, canonical)
            if replayed is not None:
                answer = await adder.settled(owner, creator, replayed)
                await tools.auth("add")
                return {"created": False, "addition": answer}
        extra: dict[str, Any] = {}
        if artist:
            name, releases, total = await adder.of_artist(
                connection, adder.artist(artist), kinds, most
            )
            items = [release_item(release) for release in releases]
            extra = {
                "artist": name,
                "release_types": kinds,
                "releases_of_these_types": total,
                "left_out_by_max_releases": total - len(items),
            }
            if not items:
                raise RequestError(
                    "The catalog lists no releases of these types for this artist. Other "
                    "release_types may: album, ep, single, compilation.",
                    404,
                )
        else:
            cids = [adder.album(text) for text in albums]
            items = (
                await adder.described(cids) if preview else [{"album_id": str(cid)} for cid in cids]
            )
        standing = await tools.service.daily_standing(owner)
        if standing:  # where the account stands with the daily cap, while one applies
            extra["added_releases_per_day"] = standing
        if preview:
            await tools.auth("add")
            return {
                "preview": True,
                "releases": items,
                # What this preview lists, to add exactly that: an artist's releases are
                # looked up anew by a call that names the artist.
                "album_ids": [item["album_id"] for item in items if "state" not in item],
                **extra,
                "note": "Nothing was added. Each release adds its whole album to the "
                "library as placeholders, which uses the server's disk once its audio is "
                "played or prepared. To add what the user agreed to, call add_to_library "
                "with these album_ids (or the ones of them the user chose) and an "
                "idempotency_key, not with the artist_id again: the catalog's list of an "
                "artist can change between two calls.",
            }
        for item in items:
            item["state"] = "pending"
        async with tools.service.changing():
            await tools.auth("add")
            ident, created = await adder.accept(owner, creator, key, canonical, items)
        answer = await adder.settled(owner, creator, ident)
        await tools.auth("add")
        return {"created": created, "addition": {**answer, **extra}}

    async def status(self, ident: str) -> dict[str, Any]:
        tools = self.tools
        token = await tools.auth()
        if not {"add", "status"} & set(token.scopes):
            await tools.auth("status")  # (refused: says which permission is missing)
        adder = self.service()
        answer = await adder.status(str(token.subject), tools.grant(token), ident)
        await tools.auth()
        return {"addition": answer}


def register_library_tools(mcp: MCPServer, tools: Tools) -> None:
    library = tools.adding
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

    @mcp.tool(annotations=read)
    async def search_catalog(query: str, limit: int = 10) -> CallToolResult:
        """Search the catalog for albums and artists to add to the user's library, with the
        add permission: up to 25 albums (one edition of each, with `album_id`, title,
        artist, type, release date and track count) and artists (`artist_id`, name), in the
        catalog's order. It reads nothing personal and says nothing about what the library
        already has. Use search_library for songs the user has; use this only to find what
        add_to_library should add.
        """
        return await tools.guarded(lambda: library.search(query, limit))

    @mcp.tool(annotations=write)
    async def add_to_library(
        album_ids: list[str] | None = None,
        artist_id: str = "",
        release_types: list[Kind] | None = None,
        max_releases: int | None = None,
        preview: bool = False,
        idempotency_key: str = "",
    ) -> CallToolResult:
        """Add catalog albums, or an artist's releases, to the user's library, only when the
        user asks for it, with the add permission. Name up to 25 `album_ids` (from
        search_catalog, get_new_releases or a preview), or one `artist_id`. An artist
        stands for the releases the artist's page in the user's apps shows, one edition of
        each album: albums only unless `release_types` names others (album, ep, single,
        compilation), the newest first, at most `max_releases` (default 10, at most 25).
        preview=true writes nothing, needs no key, and returns the `releases` and their
        `album_ids`. For an artist, preview first, show the user the list, and then add
        what they agreed to by passing those `album_ids` - not the `artist_id` again: an
        artist's releases are looked up anew by every call that names the artist, and the
        catalog's list can change in between. Add by `artist_id` without a preview only
        when the user asked for exactly that; the answer lists what was added. Without
        preview, send an idempotency_key and keep it and the same input through recovery;
        never repeat an addition under a new key after an uncertain result. Each release
        is added whole, as placeholders that play from the add-ons, and joins the user's
        Shijhon library as an album they rated or put into a playlist does; an album they
        removed from a library by hand stays out of it. The answer waits a few seconds;
        its `releases` say each one's state (added, already_in_library, not_added with a
        reason, pending), and `next` says what to call while some are pending. When an
        administrator has set a daily limit on adding, `daily_limit` says so, with how
        many releases were added and left out; tell the user, and do not retry at once.
        """
        return await tools.guarded(
            lambda: library.add(
                album_ids or [],
                artist_id,
                list(release_types or []),
                max_releases,
                preview,
                idempotency_key,
            )
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, open_world_hint=False
        )
    )
    async def get_library_addition(request_id: str) -> CallToolResult:
        """Read one addition this connection asked for with add_to_library: its state
        (queued, running, finished, stopped) and each release's outcome, with the add or
        the status permission. Use it after an answer that had releases pending, or after
        an uncertain result, instead of sending the addition again.
        """
        return await tools.guarded(lambda: library.status(request_id))
