"""What an administrator's agent may do for the whole installation: read and settle the
Library's review list, and read the server's status.

Both need the ``admin`` permission and an account that is a Navidrome administrator at
the moment of the call: asked of Navidrome each time, as the dashboard asks it for its
own pages, never remembered from the agent's sign-in. An account that was an
administrator when it connected and is none now is refused, whatever its grant says.

The review list's actions are the Library page's own (``Dashboard.settle_review``): one
album a call, with the page's checks, its one action at a time for an album, its one fill
at a time, and its background work. That work asks again whether the agent may still act
before it starts and where it writes files, so revoking the agent, taking the permission
away or demoting the account stops it.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import anyio
from markupsafe import Markup
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, ToolAnnotations

from shijhon.placeholders.engine import MaterializeError
from shijhon.playlists.model import PlaylistError, UpstreamUnavailable
from shijhon.playlists.requests import RequestError, SetupRequired

if TYPE_CHECKING:
    from collections.abc import Callable

    from shijhon.agents.tools import Tools
    from shijhon.dashboard.library import Job

ACTION_WAIT = 6.0  # the longest a review action's answer waits for its background work
# What the call's deadline keeps for the rest of its work: the check of the account at its
# end asks Navidrome, which answers slowly while it scans for the fill.
ACTION_MARGIN = 9.0
ACTION_POLL = 0.2
# Review actions' idempotency keys are kept in memory: the newest of them, for an hour
# each; a restart forgets them all. (An action under way is never forgotten.)
KEYS_KEPT = 500
KEY_SECONDS = 3600.0
UNCERTAIN = (
    "Call get_review_list with this album's ID before deciding anything again: "
    "last_action says how its last action went while that is remembered, and "
    "in_review_list whether the album still waits. Do not send the action again unless "
    "no action is running and the user decides so."
)
_KEY = re.compile(r"[A-Za-z0-9_.:-]{8,128}")
NOT_ADMIN = "the agent that asked may no longer administer this installation"
# The Library page's outcome of an action, as an agent is told it.
STATES = {
    "gone": "not_in_review_list",
    "refused": "refused",
    "busy": "busy",
    "resting": "catalog_resting",
    "kept": "kept",
    "stopping": "not_started",
}


class AdministratorRequired(RequestError):
    """The account is not a Navidrome administrator now: connecting again cannot help."""

    def __init__(self) -> None:
        super().__init__(
            "This needs a Navidrome administrator, and this account is not one now.", 403
        )


@dataclass
class Asked:
    """One review action under its idempotency key: what was asked, and the call's own
    result once it has one - for background work, that work as it stands."""

    input: str
    album_id: str
    action: str
    at: float
    done: anyio.Event  # the first call ended (with a result or without)
    result: dict[str, Any] | None = None
    job: Job | None = None  # the background work the call started


def plain(words: str) -> str:
    """A page's notice as text (a time in it is an element there)."""
    return words.striptags() if isinstance(words, Markup) else str(words)


class AdminTools:
    def __init__(self, tools: Tools, host: Any) -> None:
        self.tools, self.host = tools, host
        # Review actions by their idempotency keys, the oldest first: (owner, key).
        self._asked: dict[tuple[str, str], Asked] = {}

    async def administrator(self) -> AccessToken:
        """The call's token when it holds the admin permission and its account is a
        Navidrome administrator now."""
        token = await self.tools.auth("admin")
        if not await self.tools.oauth.administrator(str(token.subject)):
            raise AdministratorRequired
        return token

    def library(self) -> tuple[Any, Any]:
        """The dashboard and the fills, as the Library page uses them."""
        dashboard = getattr(self.host, "dashboard", None)
        services = getattr(self.host, "services", None)
        fills = getattr(services, "fills", None)
        if getattr(dashboard, "store", None) is None or fills is None:
            raise SetupRequired(
                "album_filling",
                "Filling albums is off in this installation: there is no review list.",
            )
        return dashboard, fills

    @staticmethod
    def job(job: Job) -> dict[str, Any]:
        """A review action's background work: running, or how it ended."""
        return {
            "action": job.action,
            "state": "running" if job.done is None else "finished" if job.ok else "failed",
            "message": job.message
            if job.done is not None
            else "The action is running; a fill can take a couple of minutes.",
        }

    async def review_list(self, page: int, album_id: str) -> dict[str, Any]:
        await self.administrator()
        dashboard, fills = self.library()
        if not isinstance(album_id, str) or len(album_id) > 100 or not 1 <= page <= 100_000:
            raise RequestError("Use a page from 1 or one album's ID.")
        if album_id:
            row = await dashboard._review_row(fills, album_id)
            last = dashboard.jobs.latest(album_id)
            answer: dict[str, Any] = {
                "album_id": album_id,
                "in_review_list": row is not None,
                **({"last_action": self.job(last)} if last is not None else {}),
            }
        else:
            answer = await dashboard.review_albums(fills, page)
            answer["catalog_resting"] = bool(fills.resting)
        await self.administrator()
        return answer

    def _reserve(
        self, owner: str, key: str, asked: str, album_id: str, action: str
    ) -> tuple[Asked, bool]:
        """Take the key for this input before the action starts: (its entry, whether this
        call is the one that carries the action out). Nothing is awaited here, so of two
        calls with one key only one carries an action out; other input under the key is
        refused."""
        now = time.time()
        for known in [
            k for k, v in self._asked.items() if v.done.is_set() and now - v.at > KEY_SECONDS
        ]:
            del self._asked[known]
        kept = self._asked.get((owner, key))
        if kept is not None:
            if kept.input != asked:
                raise RequestError("This request key was already used with different input.", 409)
            return kept, False
        if len(self._asked) >= KEYS_KEPT:
            oldest = next((k for k, v in self._asked.items() if v.done.is_set()), None)
            if oldest is None:
                raise RequestError("Too many review actions are under way. Try again later.", 409)
            del self._asked[oldest]
        entry = self._asked[(owner, key)] = Asked(asked, album_id, action, now, anyio.Event())
        return entry, True

    async def _wait(self, finished: Callable[[], bool]) -> None:
        """Give work under way a few seconds, within what the call's deadline leaves."""
        left = anyio.current_effective_deadline() - anyio.current_time() - ACTION_MARGIN
        with anyio.move_on_after(max(0.0, min(ACTION_WAIT, left))):
            while True:  # (a dashboard job has no event to wait on)
                if finished():
                    break
                await anyio.sleep(ACTION_POLL)

    def _answer(self, entry: Asked, *, replayed: bool = False) -> dict[str, Any]:
        """The result of the action a key stands for: its background work as it stands
        now, else what its call was answered; a first call still under way, or one that
        ended without a result, says so."""
        if entry.job is not None:
            result = self.job(entry.job)
        elif entry.result is not None:
            result = entry.result
        elif entry.done.is_set():
            result = {
                "action": entry.action,
                "state": "unknown",
                "message": "The first call with this key ended without a result.",
            }
        else:
            result = {
                "action": entry.action,
                "state": "running",
                "message": "The first call with this key is still under way.",
            }
        answer: dict[str, Any] = {"album_id": entry.album_id, "result": result}
        if replayed:
            answer["replayed"] = True
        if result["state"] in ("running", "unknown"):
            answer["next"] = UNCERTAIN
        return answer

    async def settle(self, album_id: str, action: str, release: str, key: str) -> dict[str, Any]:
        token = await self.administrator()
        owner, grant = str(token.subject), self.tools.grant(token)
        dashboard, fills = self.library()
        if (
            not isinstance(album_id, str)
            or not 0 < len(album_id) <= 100
            or not isinstance(release, str)
            or len(release) > 300
            or action not in ("fill", "keep", "rematch")
        ):
            raise RequestError("Use an album ID of the review list and fill, keep or rematch.")
        if (action == "fill") != bool(release):
            raise RequestError("Name the release to fill from with fill, and only with fill.")
        if not _KEY.fullmatch(key):
            raise RequestError("Send an idempotency_key of 8 to 128 plain characters.")
        asked = hashlib.sha256(f"{album_id}\n{action}\n{release}".encode()).hexdigest()
        # The key is taken before anything is awaited: a second call with it never starts
        # the action again, it joins this one.
        entry, first = self._reserve(owner, key, asked, album_id, action)
        if not first:
            await self._wait(
                lambda: entry.done.is_set() and (entry.job is None or entry.job.done is not None)
            )
            answer = self._answer(entry, replayed=True)
            await self.administrator()
            return answer

        async def authorize() -> None:
            """The agent may still act: its grant stands, with the permission, for an
            account that is an administrator now. The grant, not the call's token: tokens
            are replaced every hour, and a fill may outlast one."""
            try:
                connection, _ = await self.tools.service.requests.grant(grant, "admin")
                allowed = connection.user_id == owner and await self.tools.oauth.administrator(
                    owner
                )
            except UpstreamUnavailable:
                raise MaterializeError(
                    "Navidrome did not answer for the agent that asked"
                ) from None
            except PlaylistError:  # revoked, without the permission, or the account is gone
                allowed = False
            if not allowed:
                raise MaterializeError(NOT_ADMIN)

        try:
            saved = await self.tools.service.repo.connection(owner)  # (its name, for the log)
            name = saved.username if saved is not None else ""
            who = "an agent of " + "".join(ch if ch.isprintable() else "?" for ch in name)[:64]
            settled = await dashboard.settle_review(
                fills, album_id, action, release, who, authorize=authorize
            )
            if settled.job is None:
                entry.result = {
                    "action": action,
                    "state": STATES.get(settled.state, "refused"),
                    "message": plain(settled.words),
                }
            else:
                job = entry.job = settled.job
                await self._wait(lambda: job.done is not None)
        finally:
            entry.done.set()  # (without a result: the key stays taken, its outcome unknown)
        answer = self._answer(entry)
        await self.administrator()
        return answer

    async def status(self) -> dict[str, Any]:
        await self.administrator()
        dashboard: Any = getattr(self.host, "dashboard", None)
        if getattr(dashboard, "store", None) is None or getattr(dashboard, "saved", None) is None:
            raise UpstreamUnavailable("Shijhon is starting. Try again shortly.")
        answer: dict[str, Any] = await dashboard.server_status()
        await self.administrator()
        return answer


def register_admin_tools(mcp: MCPServer, tools: Tools) -> None:
    admin = tools.admin
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

    @mcp.tool(annotations=read)
    async def get_review_list(page: int = 1, album_id: str = "") -> CallToolResult:
        """For Navidrome administrators, with the admin permission: the albums in the
        Library's review list, 25 a page, as the dashboard shows them. Each has its
        `album_id`, title, artist, how many songs the library owns of how many the release
        has, why it needs a decision (`why`, `detail`) and the releases offered (`release`
        and a description; the first is the chosen one). With `album_id` instead, it says
        whether that album is still in the list (`in_review_list`) and how its last action
        went (`last_action`: running, finished or failed, remembered for about a quarter
        of an hour and until a restart; a keep leaves none): what to read after an
        uncertain settle_review_album. Owned albums and their matches belong to the whole
        installation, not to one account.
        """
        return await tools.guarded(lambda: admin.review_list(page, album_id))

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=False
        )
    )
    async def settle_review_album(
        album_id: str,
        action: Literal["fill", "keep", "rematch"],
        idempotency_key: str,
        release: str = "",
    ) -> CallToolResult:
        """For Navidrome administrators, with the admin permission, only when the user asks:
        settle one album of the review list. `fill` adds the missing songs of the named
        `release` (one of the album's offered releases) to the owned album as
        placeholders; `keep` leaves the album as it is and stops matching it; `rematch`
        forgets its match and matches it again, adding no files. One album a call; for
        several, one after another. A fill and a rematch run in the background: the answer
        waits a few seconds and its `result` says running, finished or failed, with the
        dashboard's own message. Never choose a release the user did not agree to.
        The idempotency_key stands for one action: the same key with the same input never
        starts it again and answers with that action's own result (`replayed`), also
        while the first call is still under way; other input under it is refused. Keys
        are remembered in the server's memory for one hour, the newest 500 of them, and a
        restart forgets them all: after that the same call is a new action. So after an
        uncertain result (an error, a timeout, `running` or `unknown`) do not rely on the
        key alone: call get_review_list with the album's ID first. Its `last_action` says
        how the last action went while that is remembered, and `in_review_list` whether
        the album still waits; a rematch can leave an album in the list, so being in the
        list does not mean nothing happened. Send the action again only when no action
        is running and the user decides so. A result that says nothing was started
        (refused, busy, catalog_resting, not_started, not_in_review_list) stays that
        key's result: a later attempt is a new action with a new key.
        """
        return await tools.guarded(lambda: admin.settle(album_id, action, release, idempotency_key))

    @mcp.tool(annotations=read)
    async def get_server_status() -> CallToolResult:
        """For Navidrome administrators, with the admin permission: a read-only summary of
        what the dashboard's Diagnostics page shows, to tell the user what needs them.
        Versions, whether Navidrome answers, the catalog's state, each add-on's state (an
        add-on that needs its operator to sign in names its sign-in page when that is a
        plain page address), how many albums wait in the review list, the cleanup's state,
        whether saved settings wait for a restart, and `needs_attention` in sentences. It
        holds no addresses, tokens or settings' values and nothing about any account, and
        changes nothing.
        """
        return await tools.guarded(admin.status)
