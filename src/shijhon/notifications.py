"""Notifications: states that need a person, sent to an administrator through Apprise.

One installation-wide channel: the Apprise URLs of ``[notifications] urls`` (ntfy, Discord,
Telegram, email, Home Assistant, a plain JSON webhook and many more). Six states are
reported, each from where it is found:

- an add-on says its operator must sign in again (``SourceRegistry.needs``), until it
  delivers again or is changed;
- the cleanup's daily check is blocked: what it reads as Navidrome's records is out of step
  with the library, or the library may not be written now (``Cleanup.run``);
- the usage export the cleanup asked for did not arrive (``Cleanup.run``);
- a music import waits for its person's review (``music_import_jobs``, read every minute);
- a playlist's scheduled refresh failed, which pauses its refreshes (``playlist_runs``, read
  every minute);
- the database was upgraded without the snapshot ``upgrade_snapshots`` asked for
  (``store/skipped.py``, read at startup), until an administrator dismisses it on the
  dashboard.

A notification goes out when a state begins, not again while it lasts, and again when it
ended and began anew - but never sooner than ``SPACING_SECONDS`` after the last one about
the same thing (an add-on that keeps flapping does not flood a phone). Which states last is
kept in the database (``notification_states``), so a restart sends nothing again. That a
state ended is not sent. A state that begins while nothing is to be sent (no address, its
kind switched off) is recorded as begun: it is not sent later when an address is set.

What a notification says: which state, and of one person's music only the account - never
titles, artists or playlist names - and never an address, a token or a credential. The one
link is an add-on's own sign-in page, and only a plain one (``shareable``): a link with a
query, a fragment or a user name or password could carry a credential to the services the
text passes through, so it stays on the Add-ons page and the text says so. Sending never
delays what found the state: the state is noted at once, and the notification goes out in
the background, one at a time, with a time bound, Apprise's blocking work in a worker
thread. A failure is logged without the address and shown on the
Notifications page.

Apprise is used as a library: the addresses are passed in code - no Apprise configuration
files, no attachments, no plugins loaded from disk -, its persistent storage stays in
memory, and its log (which can print addresses) is not written. The add-ons' network policy
(``delivery/netpolicy.py``) does not apply: these addresses are the administrator's own,
a service on the home network among them, and Apprise's services make their own
connections (HTTP, SMTP, MQTT...), which that policy's transport does not cover.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio

from shijhon.config import NotificationSettings

if TYPE_CHECKING:
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

    from shijhon.store import Store
    from shijhon.store.skipped import Skipped

log = logging.getLogger(__name__)

# The kinds of notification, as the settings that switch each off name them.
SIGN_IN = "addon_sign_in"
CLEANUP = "cleanup_blocked"
EXPORT = "usage_export"
IMPORT = "import_review"
PLAYLIST = "playlist_refresh"
SNAPSHOT = "snapshot_skipped"
EVENTS = (SIGN_IN, CLEANUP, EXPORT, IMPORT, PLAYLIST, SNAPSHOT)
# The least time between two notifications about the same thing.
SPACING_SECONDS = 3600.0
# One notification's whole time at its addresses (Apprise's own bound, and a little more
# for the thread); the dashboard's test waits for less.
SEND_SECONDS = 20.0
TEST_SECONDS = 12.0
# How often the states kept in the database (imports, playlist refreshes) are read.
POLL_SECONDS = 60.0
QUEUE = 256  # notifications waiting to be sent; beyond it, the newest are dropped (logged)
APP_ID = "Shijhon"
NAME_LENGTH = 100
# The pages where each state is seen, in its words.
COLLECTION = "the Collection page"


@dataclass(frozen=True)
class Notice:
    title: str
    body: str


@dataclass(frozen=True)
class Sent:
    """What became of one notification: how many of its addresses took it."""

    accepted: int
    addresses: int

    @property
    def ok(self) -> bool:
        return self.addresses > 0 and self.accepted == self.addresses

    def words(self) -> str:
        """The outcome in the dashboard's words (never an address)."""
        if self.ok:
            return "sent"
        if self.addresses == 1:
            return "not accepted by its address"
        return f"not accepted by {self.addresses - self.accepted} of its {self.addresses} addresses"


@dataclass
class State:
    event: str
    active: bool
    began_at: float
    sent_at: float | None


Maker = Callable[[], Awaitable[Notice]]  # the notice, made when it is sent (it may look up names)


def split_urls(text: str) -> list[str]:
    """The Apprise URLs in ``text``, in order, each once: separated by white space (a
    space, a new line), or by commas where the next one starts (Apprise's own rule, which
    keeps a comma inside one URL's query)."""
    from apprise.utils.parse import parse_urls

    found: list[str] = []
    for chunk in text.split():
        urls = parse_urls(chunk)  # type: ignore[no-untyped-call]
        found += [str(url) for url in urls if url] or [chunk]
    return list(dict.fromkeys(found))


def _asset(timeout: float = SEND_SECONDS) -> Any:
    """Apprise's settings for Shijhon: its name, memory-only storage, no plugin paths."""
    import apprise

    from shijhon.delivery.netpolicy import PROJECT_URL

    return apprise.AppriseAsset(
        app_id=APP_ID,
        app_desc=APP_ID,
        app_url=PROJECT_URL,
        storage_mode="memory",
        service_timeout=timeout,
    )


def unusable(addresses: list[str]) -> str:
    """Why the addresses cannot all be used ("" when they can): the first that Apprise does
    not know, by its place in the list - never the address itself (it holds a token)."""
    import apprise

    from shijhon.log import quiet_apprise

    quiet_apprise()
    asset = _asset()
    for number, url in enumerate(addresses, 1):
        if apprise.Apprise.instantiate(url, asset=asset) is None:
            which = (
                "The address" if len(addresses) == 1 else f"Address {number} of {len(addresses)}"
            )
            return (
                f"{which} is not one Apprise can send to: check its service and its parts"
                " against Apprise's list of services"
            )
    return ""


def send(addresses: list[str], notice: Notice, timeout: float = SEND_SECONDS) -> Sent:
    """Send ``notice`` to every address (blocking: run it in a worker thread). Apprise's
    exceptions and log never carry the addresses out: a failure is a count."""
    import apprise

    from shijhon.log import quiet_apprise

    quiet_apprise()  # (also where logging was not set up by the command line)
    asset = _asset(timeout)
    sender = apprise.Apprise(asset=asset)
    added = 0
    for url in addresses:
        # (An address that no longer parses is not taken; Apprise's error would name it.)
        with contextlib.suppress(Exception):
            added += bool(sender.add(url))
    if not added:
        return Sent(0, len(addresses))
    try:
        result = sender.notify(
            body=notice.body,
            title=notice.title,
            body_format=apprise.NotifyFormat.TEXT,
            timeout=timeout,
        )
        accepted = sum(1 for each in result if bool(each))
    except Exception:
        accepted = 0
    return Sent(min(accepted, added), len(addresses))


def named(text: str) -> str:
    """A name fit for a notification: one line, printable, not too long."""
    shown = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return shown[:NAME_LENGTH] or "?"


def _accounts(names: list[str]) -> str:
    quoted = [f"“{named(name)}”" for name in dict.fromkeys(names)]
    if len(quoted) == 1:
        return f"the account {quoted[0]}"
    return f"the accounts {', '.join(quoted[:-1])} and {quoted[-1]}"


def shareable(page: str) -> bool:
    """Whether an add-on's sign-in page may go into a notification, a text that leaves the
    server for other services: a plain http(s) address only - no query, no fragment, no user
    name or password, any of which could carry a credential."""
    try:
        parts = urlsplit(page)
        username, password = parts.username, parts.password
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and username is None
        and password is None
        and "@" not in parts.netloc
        and not any(mark in page for mark in "?#")
        and page.isprintable()
        and not any(ch.isspace() for ch in page)
    )


def sign_in_notice(name: str, page: str | None) -> Notice:
    if page and shareable(page):
        where = f" Its page for that: {page}"
    elif page:
        where = (
            " Its page for that is linked on the Add-ons page of Shijhon's dashboard (the"
            " link is not sent here: it may hold a credential)."
        )
    else:
        where = " The Add-ons page on Shijhon's dashboard shows it."
    return Notice(
        "Shijhon: an add-on needs sign-in",
        f"The add-on “{named(name)}” says its operator must sign in again.{where}",
    )


CLEANUP_NOTICE = Notice(
    "Shijhon: the cleanup is blocked",
    "The cleanup took nothing out: what it reads as Navidrome's records does not match the"
    " library, or the library may not be written now. The Cleanup page on Shijhon's dashboard"
    " says why; the cleanup tries again at its next check.",
)
EXPORT_NOTICE = Notice(
    "Shijhon: the usage export is out of date",
    "The cleanup asked for a new usage export and none arrived in time: is the exporter"
    " running? Until one arrives, the cleanup cannot see who uses what and takes nothing out.",
)


def import_notice(accounts: list[str]) -> Notice:
    if len(accounts) == 1:
        return Notice(
            "Shijhon: a music import waits for review",
            f"An import of {_accounts(accounts)} is checked and waits for its review, on"
            f" {COLLECTION} under Import music.",
        )
    return Notice(
        "Shijhon: music imports wait for review",
        f"{len(accounts)} imports, of {_accounts(accounts)}, are checked and wait for their"
        f" review, on {COLLECTION} under Import music.",
    )


def playlist_notice(accounts: list[str]) -> Notice:
    if len(accounts) == 1:
        return Notice(
            "Shijhon: a playlist refresh failed",
            f"A scheduled refresh of a playlist of {_accounts(accounts)} failed, and its"
            " refreshes are paused. The Playlists page says why and can try it again.",
        )
    return Notice(
        "Shijhon: playlist refreshes failed",
        f"Scheduled refreshes of {len(accounts)} playlists, of {_accounts(accounts)}, failed,"
        " and their refreshes are paused. The Playlists page says why and can try them again.",
    )


def snapshot_notice(upgraded: Skipped) -> Notice:
    """The upgrade without its snapshot: the schemas and why, in words that hold no path."""
    title = (
        "Shijhon: the database may be damaged"
        if upgraded.damaged
        else "Shijhon: the database was upgraded without a snapshot"
    )
    return Notice(
        title,
        f"Shijhon upgraded its database from schema {upgraded.from_schema} to"
        f" {upgraded.to_schema} without a snapshot: {upgraded.why()}. {upgraded.advice()}"
        " The dashboard shows this to administrators until one dismisses it.",
    )


TEST_NOTICE = Notice(
    "Shijhon: a test notification",
    "This is a test from Shijhon's dashboard. Notifications like it come when something"
    " needs a person: an add-on that needs sign-in, a blocked cleanup, a usage export that is"
    " out of date, a music import waiting for review, a playlist refresh that failed, or an"
    " upgrade of the database that went on without its snapshot.",
)

Names = Callable[[str], Awaitable[str | None]]  # a Navidrome user ID's account name


class Notices:
    """The installation's notifications: the states that last, what to send, and sending.

    ``begin`` and ``end`` are called where a state is found, from any task: they decide at
    once (from the states in memory, read from the database at startup by ``load``) and
    leave the writing and sending to ``run``, the one background task. ``settings`` is read
    at every decision: a change on the dashboard applies at once."""

    def __init__(
        self,
        store: Store,
        settings: NotificationSettings,
        *,
        names: Names | None = None,
        clock: Callable[[], float] = time.time,
        sender: Callable[[list[str], Notice, float], Sent] = send,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.store = store
        self.settings = settings
        self.names = names
        self.clock = clock
        self.sender = sender
        self.poll_seconds = poll_seconds
        self.states: dict[str, State] = {}
        # What the dashboard says: the last notification that went out and the last that
        # could not be sent (when, and the outcome's words).
        self.last_sent: float | None = None
        self.last_failed: tuple[float, str] | None = None
        self._queue: tuple[
            MemoryObjectSendStream[tuple[str, State | None, Maker | None]],
            MemoryObjectReceiveStream[tuple[str, State | None, Maker | None]],
        ] = anyio.create_memory_object_stream(QUEUE)
        self._sending = anyio.Lock()  # one notification at a time (the test too)
        self._loaded = False

    async def load(self) -> None:
        """The states kept from before (``begin`` and ``end`` decide by them)."""
        rows = await self.store.fetchall(
            "SELECT key, event, active, began_at, sent_at FROM notification_states"
        )
        self.states = {
            str(row["key"]): State(
                str(row["event"]),
                bool(row["active"]),
                float(row["began_at"]),
                None if row["sent_at"] is None else float(row["sent_at"]),
            )
            for row in rows
        }
        self._loaded = True

    def wanted(self, event: str) -> bool:
        """Whether a notification of this kind is sent now: an address is set and the kind
        is on."""
        return self.settings.urls is not None and bool(getattr(self.settings, event, False))

    # --- states, where they are found ------------------------------------------------

    def begin(self, key: str, event: str, notice: Notice | Maker | None) -> bool:
        """The state ``key`` (of kind ``event``) is found now. Returns whether a notification
        about it is to go out: when it is new (not lasting from before), wanted, and none went
        out about it within ``SPACING_SECONDS``. It is queued with ``notice``; with None the
        caller sends it (``poll``: several of a kind as one)."""
        if not self._loaded:
            return False  # (before startup read the states: nothing is decided)
        now = self.clock()
        found = self.states.get(key)
        if found is not None and found.active:
            return False
        state = State(event, True, now, found.sent_at if found is not None else None)
        due = self.wanted(event) and (
            state.sent_at is None or now - state.sent_at >= SPACING_SECONDS
        )
        if due:
            state.sent_at = now
        self.states[key] = state
        maker: Maker | None = None
        if due and notice is not None:
            maker = notice if callable(notice) else partial(_ready, notice)
        self._put(key, state, maker)
        return due

    def end(self, key: str) -> None:
        """The state ``key`` no longer lasts (nothing is sent about that)."""
        found = self.states.get(key)
        if found is None or not found.active:
            return
        found.active = False
        self._put(key, found, None)

    def _put(self, key: str, state: State | None, maker: Maker | None) -> None:
        try:
            self._queue[0].send_nowait((key, State(**vars(state)) if state else None, maker))
        except anyio.WouldBlock:
            log.warning("notifications: too many waiting; one about %s was dropped", key)

    # --- the states as their own modules report them -----------------------------------

    def addon_needs_sign_in(self, source_id: int, name: str, page: str | None) -> None:
        self.begin(f"addon:{source_id}", SIGN_IN, sign_in_notice(name, page))

    def addon_answered(self, source_id: int) -> None:
        self.end(f"addon:{source_id}")

    def cleanup_checked(self, *, export_late: bool, blocked: bool | None) -> None:
        """A daily check of the cleanup ended: whether the usage export it asked for came
        late (or not at all), and whether what it read blocked it (None: not known - the
        check did not get that far)."""
        if export_late:
            self.begin("cleanup:export", EXPORT, EXPORT_NOTICE)
        else:
            self.end("cleanup:export")
        if blocked:
            self.begin("cleanup:blocked", CLEANUP, CLEANUP_NOTICE)
        elif blocked is not None:
            self.end("cleanup:blocked")

    def upgraded_without_snapshot(self, upgraded: Skipped | None) -> None:
        """At startup, the record of an upgrade without its snapshot (None: there is none);
        dismissed on the dashboard, None again. It lasts while the record does, so it is
        sent once, by the first start that finds it."""
        if upgraded is None:
            self.end("upgrade:snapshot")
        else:
            self.begin("upgrade:snapshot", SNAPSHOT, snapshot_notice(upgraded))

    # --- the background task -------------------------------------------------------------

    async def run(self) -> None:
        """Write the states and send what is due, as they come; read the states the database
        keeps (imports, playlist refreshes) every ``poll_seconds``."""
        receive = self._queue[1]
        next_poll = time.monotonic()
        while True:
            item: tuple[str, State | None, Maker | None] | None = None
            if time.monotonic() < next_poll:
                with anyio.move_on_after(next_poll - time.monotonic()):
                    item = await receive.receive()
            if item is not None:
                await self._handle(item)
                continue
            try:
                await self.poll()
            except Exception as exc:  # (the next poll tries again)
                log.warning("notifications: reading the states failed: %s", type(exc).__name__)
            next_poll = time.monotonic() + self.poll_seconds

    async def _handle(self, item: tuple[str, State | None, Maker | None]) -> None:
        key, state, maker = item
        await self._write(key, state)
        if maker is not None:
            await self._deliver(maker)

    async def _write(self, key: str, state: State | None) -> None:
        try:
            if state is None:
                await self.store.execute("DELETE FROM notification_states WHERE key=?", [key])
                return
            await self.store.execute(
                "INSERT INTO notification_states (key, event, active, began_at, sent_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET event=excluded.event,"
                " active=excluded.active, began_at=excluded.began_at, sent_at=excluded.sent_at",
                [key, state.event, int(state.active), state.began_at, state.sent_at],
            )
        except Exception as exc:  # (kept in memory: written with its next change)
            log.warning("notifications: a state could not be saved: %s", type(exc).__name__)

    async def _deliver(self, maker: Maker) -> Sent | None:
        try:
            notice = await maker()
        except Exception as exc:
            log.warning("notifications: a notification could not be made: %s", type(exc).__name__)
            return None
        return await self.deliver(notice, SEND_SECONDS)

    async def deliver(self, notice: Notice, seconds: float) -> Sent:
        """Send ``notice`` to the addresses set now (the outcome is logged and kept for the
        dashboard, never an address)."""
        addresses = self.settings.addresses()
        if not addresses:
            return Sent(0, 0)
        async with self._sending:
            outcome: Sent | None = None
            # (Apprise bounds its own work by ``seconds``; the thread is left behind if it
            # does not keep to it.)
            with anyio.move_on_after(seconds + 5):
                outcome = await anyio.to_thread.run_sync(
                    partial(self.sender, addresses, notice, seconds), abandon_on_cancel=True
                )
        if outcome is None:
            outcome = Sent(0, len(addresses))
            words = "it took too long"
        else:
            words = outcome.words()
        now = self.clock()
        if outcome.ok:
            self.last_sent = now
        else:
            self.last_failed = (now, words)
            log.warning("notifications: a notification could not be sent: %s", words)
        return outcome

    async def test(self) -> Sent:
        """The dashboard's "Send a test notification": sent now, with its outcome."""
        return await self.deliver(TEST_NOTICE, TEST_SECONDS)

    # --- the states the database keeps -----------------------------------------------------

    async def poll(self) -> None:
        """Begin and end the states of music imports and playlist refreshes as the database
        has them now; the newly begun of one kind go out as one notification."""
        await self._sync(
            "import:",
            IMPORT,
            await self.store.fetchall(
                "SELECT id AS ident, owner_id AS owner, '' AS username FROM music_import_jobs"
                " WHERE state='review'"
            ),
            import_notice,
        )
        # A playlist whose latest run is a scheduled refresh that failed (or whose result is
        # uncertain): its refreshes are paused by it, until it runs again or they are on.
        await self._sync(
            "playlist:",
            PLAYLIST,
            await self.store.fetchall(
                "SELECT m.id AS ident, m.owner AS owner, COALESCE(o.username, '') AS username"
                " FROM playlist_mixes m"
                " JOIN playlist_runs r ON r.id = (SELECT id FROM playlist_runs"
                "   WHERE mix = m.id ORDER BY created DESC LIMIT 1)"
                " LEFT JOIN playlist_owners o ON o.id = m.owner"
                " WHERE m.enabled = 0 AND r.automatic = 1 AND r.state IN ('failed', 'uncertain')"
            ),
            playlist_notice,
        )
        await self._forget()

    async def _sync(
        self,
        prefix: str,
        event: str,
        rows: Iterable[Any],
        make: Callable[[list[str]], Notice],
    ) -> None:
        found = {
            prefix + str(row["ident"]): (str(row["owner"]), str(row["username"])) for row in rows
        }
        for key, state in list(self.states.items()):
            if key.startswith(prefix) and state.active and key not in found:
                self.end(key)
                await self._drain()  # (each written as it goes: many never fill the queue)
        due = []
        for key, owner in found.items():
            if self.begin(key, event, None):
                due.append(owner)
            await self._drain()
        if not due:
            return

        async def notice() -> Notice:
            return make([username or await self._name(owner) for owner, username in due])

        await self._deliver(notice)

    async def _drain(self) -> None:
        """Handle what is queued now: the states ``poll`` just changed are written before
        what is sent about them."""
        receive = self._queue[1]
        while True:
            try:
                item = receive.receive_nowait()
            except anyio.WouldBlock:
                return
            await self._handle(item)

    async def _name(self, user_id: str) -> str:
        if self.names is not None:
            try:
                with anyio.fail_after(5):
                    found = await self.names(user_id)
                if found:
                    return found
            except Exception as exc:
                log.debug("notifications: an account name was not found: %s", type(exc).__name__)
        return "?"

    async def _forget(self) -> None:
        """States that ended and can no longer hold a notification back go."""
        now = self.clock()
        for key, state in list(self.states.items()):
            if state.active or (
                state.sent_at is not None and now - state.sent_at < SPACING_SECONDS
            ):
                continue
            del self.states[key]
            await self._write(key, None)


async def _ready(notice: Notice) -> Notice:
    return notice
