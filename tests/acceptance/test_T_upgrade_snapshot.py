"""The snapshot before an upgrade, for real: ``shijhon serve`` as a process of its own, in
front of a real Navidrome, started on a database an earlier Shijhon left at an older schema.

Shown: the start writes a checked snapshot of the old schema into ``snapshots/`` beside the
database, then upgrades and answers; a later start takes none. When the snapshot cannot be
written, the start logs why, upgrades and answers all the same; administrators - and only
they - see it on the dashboard, across restarts, until one dismisses it; and the
notification addresses get it once, with the schemas and why and no path.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import pytest

from shijhon.store import backup
from shijhon.store.db import apply_migrations, latest_schema, load_migrations
from tests.acceptance.test_T_restart import Served
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import PATH, Browser
from tests.harness.notification_sink import TOKEN, NotificationSink

MIGRATIONS = load_migrations(Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations")
OLD = 30
TAKEN = f"database snapshot before the upgrade from schema {OLD} to {latest_schema()}"
WENT_ON = "could not be written, so the upgrade goes on without one"
ALERT = "Upgraded without a snapshot."
LISTENER, LISTENER_PASSWORD = "listener", "listener-password-for-tests"


def old_database(tmp: Path) -> Path:
    database = tmp / "state" / "shijhon.sqlite3"
    database.parent.mkdir(mode=0o700)
    conn = sqlite3.connect(database, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    apply_migrations(conn, MIGRATIONS[:OLD])
    conn.close()
    database.chmod(0o600)
    return database


def schema(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_the_first_start_of_an_upgrade_takes_a_snapshot_and_later_starts_none(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    database = old_database(tmp_path)
    served = Served(tmp_path, navidrome_factory())
    try:
        first = served.run()
        log = served.said(TAKEN)
        assert "startup failed" not in log
        [made] = backup.snapshots(database.parent / "snapshots")
        assert schema(made.path) == OLD and schema(database) == latest_schema()
        served.stop()
        served.start()
        served.run(other_than=first)
        assert backup.snapshots(database.parent / "snapshots") == [made]
        assert served.log.read_text().count(TAKEN) == 1
    finally:
        served.stop()


def alert(browser: Browser, page: str = "addons") -> str:
    """The alert's text on a page ("" when it is not there)."""
    found = re.search(
        r'<div class="banner" role="status">\s*<p><strong>Upgraded without.*?</div>',
        browser.get(page).text,
        re.S,
    )
    return re.sub(r"<[^>]+>", "", found.group(0)) if found else ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into read-only folders")
def test_a_snapshot_that_cannot_be_written_does_not_stop_the_start_and_is_said_until_dismissed(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    sink = NotificationSink().start()
    nd = navidrome_factory()
    nd.create_user(LISTENER, LISTENER_PASSWORD)
    database = old_database(tmp_path)
    folder = database.parent / "snapshots"
    folder.mkdir(mode=0o500)
    served = Served(tmp_path, nd, more=f'\n[notifications]\nurls = "{sink.address()}"\n')
    listener = Browser(served.base_url)
    try:
        first = served.run()
        log = served.said(WENT_ON)
        assert "cannot be written" in log and "startup failed" not in log
        assert schema(database) == latest_schema() and backup.snapshots(folder) == []
        # The notification, once: the schemas and why, no path, no address.
        [received] = sink.wait(1)
        assert received.body["title"] == "Shijhon: the database was upgraded without a snapshot"
        message = received.body["message"]
        assert f"from schema {OLD} to {latest_schema()} without a snapshot" in message
        assert "the snapshots folder in the state folder could not be written" in message
        assert str(tmp_path) not in message and TOKEN not in message
        # Administrators see it on every page, in one line with its action.
        admin = served.signed_in()
        shown = alert(admin)
        assert f"from schema {OLD} to {latest_schema()}" in shown
        assert "could not be written" in shown and "shijhon backup" in shown
        assert "Dismiss" in shown and str(tmp_path) not in shown
        assert alert(admin, "notifications") == shown
        # Someone who is not an administrator does not, and cannot dismiss it.
        assert listener.sign_in(LISTENER, LISTENER_PASSWORD).status_code == 303
        assert ALERT not in listener.get("collection").text
        refused = listener.post("upgrade-alert", {"csrf": listener.csrf()})
        assert refused.status_code == 403
        # A restart: still there, and not sent again.
        served.stop()
        served.start()
        second = served.run(other_than=first)
        admin = served.signed_in()
        assert alert(admin) == shown
        # Dismissed: gone, also after the next restart.
        done = admin.post("upgrade-alert", {"csrf": admin.csrf(), "next": f"{PATH}/library"})
        assert done.status_code == 303 and done.headers["location"] == f"{PATH}/library"
        assert alert(admin) == ""
        served.stop()
        served.start()
        served.run(other_than=second)
        admin = served.signed_in()
        assert alert(admin) == ""
        # (A test after them all: had the state been sent again, it would have come first.)
        admin.post("notifications/test", {"csrf": admin.csrf()})
        assert [r.body["title"] for r in sink.wait(2)] == [
            "Shijhon: the database was upgraded without a snapshot",
            "Shijhon: a test notification",
        ]
    finally:
        folder.chmod(0o700)
        listener.close()
        served.stop()
        sink.stop()
