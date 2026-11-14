"""Notifications (``notifications.py``): each state is sent once when it begins, spaced out
when it comes back, kept across a restart, sent only with an address and its kind on; one
person's music is named by its account alone; addresses never reach a text, a log line or an
error; sending through Apprise reaches a local stand-in and a failure is said without the
address; the add-on registry and the cleanup report their states, and the start an upgrade
without its snapshot."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic import SecretStr, ValidationError

from shijhon import notifications as module
from shijhon.config import NotificationSettings, load_settings
from shijhon.dashboard.saved import list_saved
from shijhon.notifications import (
    CLEANUP,
    EXPORT,
    IMPORT,
    PLAYLIST,
    SIGN_IN,
    SNAPSHOT,
    SPACING_SECONDS,
    Notice,
    Notices,
    Sent,
    send,
    split_urls,
)
from shijhon.store import Store
from shijhon.store.skipped import Skipped
from tests.harness.notification_sink import TOKEN, NotificationSink

pytestmark = pytest.mark.anyio

ADDRESS = f"json://127.0.0.1:9/hook/{TOKEN}"  # (never sent to in the tests that use it)


class Recorder:
    """A sender that records what it was given, and answers as told."""

    def __init__(self) -> None:
        self.sent: list[tuple[list[str], Notice]] = []
        self.accept = True

    def __call__(self, addresses: list[str], notice: Notice, seconds: float) -> Sent:
        self.sent.append((list(addresses), notice))
        return Sent(len(addresses) if self.accept else 0, len(addresses))


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    opened = await Store.open(tmp_path / "state.sqlite3")
    yield opened
    await opened.close()


@pytest.fixture
def sink() -> Iterator[NotificationSink]:
    made = NotificationSink().start()
    yield made
    made.stop()


def settings(urls: str | None = ADDRESS, **events: bool) -> NotificationSettings:
    return NotificationSettings(urls=SecretStr(urls) if urls else None, **events)


async def made(
    store: Store, recorder: Recorder, clock: Clock, **kwargs: Any
) -> tuple[Notices, Recorder]:
    notices = Notices(
        store,
        kwargs.pop("config", settings()),
        clock=clock,
        sender=recorder,
        **kwargs,
    )
    await notices.load()
    return notices, recorder


async def settle(notices: Notices) -> None:
    """What ``run`` does with the queue: write the states, send what is due."""
    await notices._drain()


async def rows(store: Store) -> dict[str, tuple[str, int, float | None]]:
    found = await store.fetchall("SELECT key, event, active, sent_at FROM notification_states")
    return {r["key"]: (r["event"], r["active"], r["sent_at"]) for r in found}


# --- once when it begins -------------------------------------------------------------------


async def test_a_state_is_sent_once_when_it_begins_and_not_while_it_lasts(store: Store) -> None:
    clock = Clock()
    notices, sent = await made(store, Recorder(), clock)
    notices.addon_needs_sign_in(4, "Night Shift", "https://addon.example.invalid/login")
    await settle(notices)
    assert len(sent.sent) == 1
    addresses, notice = sent.sent[0]
    assert addresses == [ADDRESS]
    assert "Night Shift" in notice.body and "https://addon.example.invalid/login" in notice.body
    assert TOKEN not in notice.title + notice.body
    # Found again (every play that asks it) while it lasts: nothing more.
    for _ in range(3):
        clock.now += 10
        notices.addon_needs_sign_in(4, "Night Shift", "https://addon.example.invalid/login")
    await settle(notices)
    assert len(sent.sent) == 1
    assert (await rows(store))["addon:4"][:2] == (SIGN_IN, 1)


async def test_one_that_ends_and_begins_again_is_spaced_out(store: Store) -> None:
    clock = Clock()
    notices, sent = await made(store, Recorder(), clock)
    notices.addon_needs_sign_in(4, "Night Shift", None)
    notices.addon_answered(4)  # it delivered
    clock.now += 60
    notices.addon_needs_sign_in(4, "Night Shift", None)  # a flapping add-on: held back
    await settle(notices)
    assert len(sent.sent) == 1
    notices.addon_answered(4)
    clock.now += SPACING_SECONDS
    notices.addon_needs_sign_in(4, "Night Shift", None)  # an hour after the last one
    await settle(notices)
    assert len(sent.sent) == 2
    assert "The Add-ons page" in sent.sent[1][1].body  # (no page given: where it is shown)


PLAIN = "https://addon.example.invalid/account/sign-in"
WITHHELD = (
    f"https://addon.example.invalid/sign-in?session={TOKEN}",  # a query
    f"https://addon.example.invalid/sign-in#{TOKEN}",  # a fragment
    f"https://operator:{TOKEN}@addon.example.invalid/sign-in",  # a user and a password
    f"https://{TOKEN}@addon.example.invalid/sign-in",  # a user alone
    "https://addon.example.invalid/sign-in?",  # (an empty query is still one)
)


def test_a_plain_sign_in_page_is_linked() -> None:
    notice = module.sign_in_notice("Night Shift", PLAIN)
    assert notice.body == (
        f"The add-on “Night Shift” says its operator must sign in again. Its page for that: {PLAIN}"
    )


@pytest.mark.parametrize("page", WITHHELD)
def test_a_sign_in_page_that_could_hold_a_credential_stays_on_the_dashboard(page: str) -> None:
    notice = module.sign_in_notice("Night Shift", page)
    assert notice.body == (
        "The add-on “Night Shift” says its operator must sign in again. Its page for that is"
        " linked on the Add-ons page of Shijhon's dashboard (the link is not sent here: it may"
        " hold a credential)."
    )
    assert TOKEN not in notice.title + notice.body and "addon.example" not in notice.body


def test_without_a_page_the_add_ons_page_is_named() -> None:
    notice = module.sign_in_notice("Night Shift", None)
    assert notice.body.endswith(
        "must sign in again. The Add-ons page on Shijhon's dashboard shows it."
    )


async def test_a_withheld_link_does_not_reach_the_service(
    store: Store, sink: NotificationSink
) -> None:
    notices = Notices(store, settings(sink.address("hook-token")))
    await notices.load()
    for page in (WITHHELD[0], PLAIN):
        notices.addon_needs_sign_in(len(page), "Night Shift", page)
        await settle(notices)
    messages = [r.body["message"] for r in sink.wait(2)]
    assert TOKEN not in messages[0] and "linked on the Add-ons page" in messages[0]
    assert messages[1].endswith(f"Its page for that: {PLAIN}")


async def test_what_lasts_is_kept_across_a_restart(store: Store) -> None:
    clock = Clock()
    notices, sent = await made(store, Recorder(), clock)
    notices.cleanup_checked(export_late=True, blocked=True)
    await settle(notices)
    assert [n.title for _, n in sent.sent] == [
        module.EXPORT_NOTICE.title,
        module.CLEANUP_NOTICE.title,
    ]
    clock.now += 2 * SPACING_SECONDS
    again, sent_again = await made(store, Recorder(), clock)  # Shijhon started again
    again.cleanup_checked(export_late=True, blocked=True)  # still so
    await settle(again)
    assert sent_again.sent == []
    again.cleanup_checked(export_late=False, blocked=False)  # it ended
    await settle(again)
    assert {key: active for key, (_, active, _) in (await rows(store)).items()} == {
        "cleanup:export": 0,
        "cleanup:blocked": 0,
    }


async def test_a_check_that_did_not_get_far_leaves_the_blocked_state_alone(store: Store) -> None:
    notices, sent = await made(store, Recorder(), Clock())
    notices.cleanup_checked(export_late=False, blocked=True)
    notices.cleanup_checked(export_late=True, blocked=None)  # (no listing: not known)
    await settle(notices)
    assert notices.states["cleanup:blocked"].active
    assert [n.title for _, n in sent.sent] == [
        module.CLEANUP_NOTICE.title,
        module.EXPORT_NOTICE.title,
    ]


async def test_nothing_is_sent_without_an_address_or_with_the_kind_off(store: Store) -> None:
    clock = Clock()
    notices, sent = await made(store, Recorder(), clock, config=settings(None))
    notices.addon_needs_sign_in(1, "A", None)
    await settle(notices)
    assert sent.sent == [] and notices.states["addon:1"].active  # noted all the same
    # An address set later does not send what began before.
    notices.settings = settings()
    notices.addon_needs_sign_in(1, "A", None)
    await settle(notices)
    assert sent.sent == []
    notices.settings = settings(addon_sign_in=False)
    notices.addon_needs_sign_in(2, "B", None)
    notices.cleanup_checked(export_late=True, blocked=None)
    await settle(notices)
    assert [n.title for _, n in sent.sent] == [module.EXPORT_NOTICE.title]


async def test_ended_states_are_forgotten_once_they_hold_nothing_back(store: Store) -> None:
    clock = Clock()
    notices, _ = await made(store, Recorder(), clock)
    notices.addon_needs_sign_in(1, "A", None)
    notices.addon_answered(1)
    await settle(notices)
    await notices._forget()
    assert "addon:1" in await rows(store)  # (an hour has not passed: it still spaces)
    clock.now += SPACING_SECONDS
    await notices._forget()
    assert await rows(store) == {} and notices.states == {}


# --- the states the database keeps -----------------------------------------------------------


async def add_import(store: Store, job: str, owner: str, state: str) -> None:
    await store.execute(
        "INSERT INTO music_import_jobs (id, owner_id, library_id, root_id, config_id,"
        " request_key, state, created) VALUES (?, ?, '1', 'r', 'c', ?, ?, 0)",
        [job, owner, job, state],
    )


async def add_playlist(
    store: Store, mix: str, owner: str, name: str, runs: list[tuple[str, int]], *, enabled: int = 0
) -> None:
    await store.execute(
        "INSERT OR IGNORE INTO playlist_owners (id, username, connection, generation)"
        " VALUES (?, ?, '{}', '')",
        [owner, name],
    )
    await store.execute(
        "INSERT INTO playlist_mixes (id, owner, definition, enabled) VALUES (?, ?, ?, ?)",
        [mix, owner, '{"title": "Secret Garden Mix"}', enabled],
    )
    for n, (state, automatic) in enumerate(runs):
        await store.execute(
            "INSERT INTO playlist_runs (id, mix, created, state, automatic, error)"
            " VALUES (?, ?, ?, ?, ?, 'The connection changed.')",
            [f"{mix}-{n}", mix, float(n), state, automatic],
        )


async def test_imports_waiting_for_review_name_the_accounts_only(store: Store) -> None:
    names = {"u-1": "alice", "u-2": "bob"}

    async def name(user_id: str) -> str | None:
        return names.get(user_id)

    notices, sent = await made(store, Recorder(), Clock(), names=name)
    await add_import(store, "job-1", "u-1", "review")
    await add_import(store, "job-2", "u-2", "review")
    await add_import(store, "job-3", "u-1", "checking")
    await notices.poll()
    assert len(sent.sent) == 1  # the two that began together: one notification
    notice = sent.sent[0][1]
    assert "alice" in notice.body and "bob" in notice.body and "2 imports" in notice.body
    await notices.poll()
    assert len(sent.sent) == 1
    await store.execute("UPDATE music_import_jobs SET state='approved' WHERE id='job-1'")
    await store.execute("UPDATE music_import_jobs SET state='review' WHERE id='job-3'")
    await notices.poll()
    assert not notices.states["import:job-1"].active
    assert len(sent.sent) == 2 and "An import of the account “alice”" in sent.sent[1][1].body
    assert (await rows(store))["import:job-3"][:2] == (IMPORT, 1)


async def test_a_failed_refresh_that_paused_a_playlist_is_sent_without_its_name(
    store: Store,
) -> None:
    notices, sent = await made(store, Recorder(), Clock())
    await add_playlist(store, "mix-1", "u-1", "alice", [("done", 1), ("failed", 1)])
    await add_playlist(store, "mix-2", "u-1", "alice", [("failed", 0)])  # someone's own run
    await add_playlist(store, "mix-3", "u-2", "bob", [("failed", 1), ("done", 0)])  # retried
    await add_playlist(store, "mix-4", "u-2", "bob", [("failed", 1)], enabled=1)  # turned on
    await notices.poll()
    assert len(sent.sent) == 1
    notice = sent.sent[0][1]
    assert "the account “alice”" in notice.body and "paused" in notice.body
    assert "Secret Garden" not in notice.body and "connection changed" not in notice.body
    assert set(notices.states) == {"playlist:mix-1"}
    await store.execute("UPDATE playlist_mixes SET enabled=1 WHERE id='mix-1'")
    await notices.poll()
    assert not notices.states["playlist:mix-1"].active
    assert (await rows(store))["playlist:mix-1"][:2] == (PLAYLIST, 0)


async def test_many_that_begin_together_are_all_kept_and_sent_as_one(store: Store) -> None:
    notices, sent = await made(store, Recorder(), Clock())
    for n in range(module.QUEUE + 44):
        await add_import(store, f"job-{n}", "u-1", "review")
    await notices.poll()
    assert len(sent.sent) == 1 and f"{module.QUEUE + 44} imports" in sent.sent[0][1].body
    assert len(await rows(store)) == module.QUEUE + 44  # (none dropped from the queue)


async def test_an_account_name_that_cannot_be_found_is_not_guessed(store: Store) -> None:
    async def broken(user_id: str) -> str | None:
        raise RuntimeError("Navidrome is away")

    notices, sent = await made(store, Recorder(), Clock(), names=broken)
    await add_import(store, "job-9", "u-9", "review")
    await notices.poll()
    assert "the account “?”" in sent.sent[0][1].body and "u-9" not in sent.sent[0][1].body


# --- sending ---------------------------------------------------------------------------------


async def test_a_notification_reaches_the_address_through_apprise(
    store: Store, sink: NotificationSink
) -> None:
    notices = Notices(store, settings(sink.address()))
    await notices.load()
    outcome = await notices.deliver(module.sign_in_notice("Night Shift", None), 5)
    assert outcome.ok and notices.last_sent is not None and notices.last_failed is None
    [received] = sink.wait(1)
    assert received.path == f"/hook/{TOKEN}"
    assert received.body["title"] == "Shijhon: an add-on needs sign-in"
    assert "Night Shift" in received.body["message"]
    assert received.body["attachments"] == []
    assert TOKEN not in received.body["title"] + received.body["message"]


async def test_a_failure_is_counted_and_logged_without_the_address(
    store: Store, sink: NotificationSink, caplog: pytest.LogCaptureFixture
) -> None:
    from shijhon.log import quiet_libraries

    quiet_libraries()
    caplog.set_level(logging.DEBUG)
    sink.status = 500
    two = f"{sink.address()} {sink.address('second-' + TOKEN)}"
    notices = Notices(store, settings(two))
    await notices.load()
    outcome = await notices.test()
    assert not outcome.ok and outcome.words() == "not accepted by 2 of its 2 addresses"
    assert notices.last_failed is not None and notices.last_failed[1] == outcome.words()
    assert "could not be sent" in caplog.text
    assert TOKEN not in caplog.text and "127.0.0.1" not in caplog.text
    sink.stop()  # nothing answers: still a count
    gone = send([sink.address()], module.TEST_NOTICE, 2)
    assert gone == Sent(0, 1)


async def test_sending_never_holds_up_what_found_the_state(store: Store) -> None:
    started = anyio.Event()
    release = anyio.Event()

    def slow(addresses: list[str], notice: Notice, seconds: float) -> Sent:
        anyio.from_thread.run_sync(started.set)
        anyio.from_thread.run(release.wait)
        return Sent(1, 1)

    notices = Notices(store, settings(), sender=slow, poll_seconds=3600)
    await notices.load()
    async with anyio.create_task_group() as group:
        group.start_soon(notices.run)
        notices.addon_needs_sign_in(1, "A", None)  # returns at once
        with anyio.fail_after(5):
            await started.wait()
        notices.addon_needs_sign_in(2, "B", None)  # while one is being sent: at once too
        assert notices.states["addon:2"].active
        release.set()
        group.cancel_scope.cancel()


# --- the setting ----------------------------------------------------------------------------


def test_addresses_are_split_and_checked_without_repeating_them() -> None:
    assert split_urls(f"ntfy://topic,json://127.0.0.1:8/x\n{ADDRESS} ntfy://topic") == [
        "ntfy://topic",
        "json://127.0.0.1:8/x",
        ADDRESS,
    ]
    good = NotificationSettings(urls=SecretStr(f"ntfy://topic {ADDRESS}"))
    assert good.addresses() == ["ntfy://topic", ADDRESS]
    assert NotificationSettings(urls=SecretStr("  ")).urls is None
    with pytest.raises(ValidationError) as refused:
        NotificationSettings(urls=SecretStr(f"ntfy://topic nonsense-{TOKEN}"))
    text = str(refused.value)
    assert "Address 2 of 2 is not one Apprise can send to" in text and TOKEN not in text
    with pytest.raises(ValidationError) as unknown:
        load_settings(None, notifications={"urls": f"unknown://{TOKEN}@host"})
    assert "The address is not one Apprise can send to" in str(unknown.value)
    assert TOKEN not in str(unknown.value)


async def test_shijhon_saved_hides_the_addresses(tmp_path: Path, store: Store) -> None:
    from shijhon.dashboard.saved import SavedSettings

    await SavedSettings(store).put(
        "notifications", {"urls": SecretStr(ADDRESS), "usage_export": False}, "admin"
    )
    listed = await anyio.to_thread.run_sync(list_saved, tmp_path / "state.sqlite3")
    assert "notifications.urls = (secret)" in listed
    assert "notifications.usage_export = false" in listed and TOKEN not in listed


def test_apprise_writes_nothing_to_the_log(caplog: pytest.LogCaptureFixture) -> None:
    from shijhon.log import quiet_libraries

    # As ``configure_logging("DEBUG", ["apprise", "urllib3"])`` leaves them (without
    # replacing the handler that captures the log here).
    for name in ("apprise", "urllib3"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    quiet_libraries()
    caplog.set_level(logging.DEBUG)
    logging.getLogger("apprise").error("Failed to send to %s", ADDRESS)
    logging.getLogger("urllib3").debug("POST /hook/%s", TOKEN)
    module.unusable([f"unknown://{TOKEN}"])  # (Apprise logs what it cannot parse)
    assert TOKEN not in caplog.text


def test_the_states_table_is_a_migration_of_its_own(tmp_path: Path) -> None:
    from shijhon.store.db import prepare_database

    prepare_database(tmp_path / "s.sqlite3")
    with sqlite3.connect(tmp_path / "s.sqlite3") as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(notification_states)")]
    assert columns == ["key", "event", "active", "began_at", "sent_at"]


# --- where the states are found ------------------------------------------------------------


async def test_the_add_on_registry_reports_a_sign_in_and_its_end(store: Store) -> None:
    from shijhon.delivery.sources import SourceRegistry

    notices, sent = await made(store, Recorder(), Clock())
    sources = SourceRegistry(store)
    sources.notices = notices
    assert sources.needs(7, "sign_in", "https://addon.example.invalid/in", 0)
    sources.needs(7, "sign_in", "https://addon.example.invalid/in", 0)
    await settle(notices)
    assert len(sent.sent) == 1 and "#7" in sent.sent[0][1].body  # (no add-on listed by that ID)
    sources.answered(7)  # it delivered again
    assert not notices.states["addon:7"].active
    # A need noted before a restart ends with the first delivery after it, too.
    notices.states["addon:8"] = module.State(SIGN_IN, True, 0.0, 0.0)
    sources.answered(8)
    assert not notices.states["addon:8"].active


async def test_the_cleanups_daily_check_reports_a_late_export_and_a_refusal(
    store: Store, tmp_path: Path
) -> None:
    from shijhon.cleanup import Cleanup, Swept
    from shijhon.navidrome.usage import Mark, NotFresh

    notices, sent = await made(store, Recorder(), Clock())
    outcomes: list[Any] = [
        NotFresh(Mark(0.0), None),
        Swept(mode="on", refused="Navidrome's records do not have the songs"),
        Swept(mode="on"),
    ]
    cleanup = Cleanup(
        None,  # type: ignore[arg-type]  # (the check itself is replaced)
        database=tmp_path / "unused.sqlite3",
        usage=object(),  # type: ignore[arg-type]
        mode="on",
        interval_seconds=0.001,
        start_seconds=0,
    )
    cleanup.listing_wait = 0.001
    cleanup.notices = notices
    seen: list[dict[str, bool]] = []

    async def sweep() -> Swept:
        seen.append({key: state.active for key, state in notices.states.items()})
        if not outcomes:
            scope.cancel()
            await anyio.sleep_forever()
        found = outcomes.pop(0)
        if isinstance(found, Exception):
            raise found
        return found  # type: ignore[no-any-return]

    cleanup.sweep = sweep  # type: ignore[method-assign]
    with anyio.move_on_after(5) as scope:
        await cleanup.run()
    assert not outcomes
    assert seen[1:] == [
        {"cleanup:export": True},
        {"cleanup:export": False, "cleanup:blocked": True},
        {"cleanup:export": False, "cleanup:blocked": False},
    ]
    await settle(notices)
    assert [n.title for _, n in sent.sent] == [
        module.EXPORT_NOTICE.title,
        module.CLEANUP_NOTICE.title,
    ]
    assert {CLEANUP, EXPORT} == {state.event for state in notices.states.values()}


# --- an upgrade without its snapshot ---------------------------------------------------------

UPGRADED = Skipped(44, 45, 1_000_000.0, "no_room", needed=150 * 2**20, free=80 * 2**20)


async def test_an_upgrade_without_a_snapshot_is_sent_once_with_the_schemas_and_why(
    store: Store,
) -> None:
    clock = Clock()
    notices, sent = await made(store, Recorder(), clock)
    notices.upgraded_without_snapshot(UPGRADED)
    await settle(notices)
    [(addresses, notice)] = sent.sent
    assert addresses == [ADDRESS]
    assert notice.title == "Shijhon: the database was upgraded without a snapshot"
    assert notice.body == (
        "Shijhon upgraded its database from schema 44 to 45 without a snapshot: there was not"
        " enough free space (it needed 150.0 MB, and 80.0 MB was free). Free some space so"
        " that the next upgrade can take one, and make a backup now with shijhon backup. The"
        " dashboard shows this to administrators until one dismisses it."
    )
    assert TOKEN not in notice.title + notice.body and "/" not in notice.body
    # The next start finds the record still there: nothing more.
    clock.now += 2 * SPACING_SECONDS
    again, sent_again = await made(store, Recorder(), clock)
    again.upgraded_without_snapshot(UPGRADED)
    await settle(again)
    assert sent_again.sent == []
    # Dismissed: it ended (the start after it finds no record, and ends it too).
    again.upgraded_without_snapshot(None)
    await settle(again)
    assert (await rows(store))["upgrade:snapshot"][:2] == (SNAPSHOT, 0)


async def test_a_damaged_database_is_said_first(store: Store) -> None:
    notices, sent = await made(store, Recorder(), Clock())
    notices.upgraded_without_snapshot(Skipped(30, 45, 1.0, "damaged"))
    await settle(notices)
    [(_, notice)] = sent.sent
    assert notice.title == "Shijhon: the database may be damaged"
    assert "did not pass SQLite's integrity check, so it may be damaged" in notice.body
    assert "restore an earlier backup" in notice.body


async def test_an_upgrade_without_a_snapshot_follows_its_switch_and_the_address(
    store: Store,
) -> None:
    notices, sent = await made(store, Recorder(), Clock(), config=settings(snapshot_skipped=False))
    notices.upgraded_without_snapshot(UPGRADED)
    await settle(notices)
    assert sent.sent == [] and notices.states["upgrade:snapshot"].active
    notices.upgraded_without_snapshot(None)
    # Begun without an address: not sent once one is set.
    notices.settings = settings(None)
    notices.upgraded_without_snapshot(UPGRADED)
    notices.settings = settings()
    notices.upgraded_without_snapshot(UPGRADED)
    await settle(notices)
    assert sent.sent == []
    assert notices.wanted(SNAPSHOT) and not settings(snapshot_skipped=False).snapshot_skipped


def test_the_test_notification_names_every_kind() -> None:
    assert "without its snapshot" in module.TEST_NOTICE.body
    assert len(module.EVENTS) == 6 and SNAPSHOT in module.EVENTS
