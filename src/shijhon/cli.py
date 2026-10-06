"""Command line: ``shijhon serve``; ``shijhon matches`` lists the album matches (the library
pass's dry run, the review list) from the database without changing it; ``shijhon cleanup``
lists what the cleanup of unused releases would take out now (read only);
``shijhon catalogs`` lists the installed catalog adapters;
``shijhon usage-export`` writes what the cleanup reads of Navidrome's database, for a
Shijhon that does not see that database itself.

``serve`` exits with ``RESTART_STATUS`` when the dashboard's "Restart Shijhon" stopped it:
whatever runs Shijhon starts it again (``Restart``)."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from threading import Timer

import anyio
import uvicorn
from pydantic import ValidationError

from shijhon import __version__, cleanup
from shijhon.app import create_app
from shijhon.catalog import plugin
from shijhon.config import Settings, load_settings
from shijhon.dashboard.saved import clear_saved, effective_from_database, list_saved
from shijhon.fill import undo
from shijhon.fill.fills import FillPolicy
from shijhon.fill.report import ReportError, report
from shijhon.log import configure_logging
from shijhon.navidrome import export
from shijhon.navidrome.checks import PlaceholderWrites
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.export import Snapshot
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.navidrome.usage import UsageSource, open_read_only
from shijhon.navidrome.usage import configured as usage_configured
from shijhon.placeholders.engine import PlaceholderEngine
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.silence import SilenceMaker
from shijhon.store import Store
from shijhon.store.writer import AlreadyRunning, WriterLock

log = logging.getLogger(__name__)
# The exit status of a ``serve`` that the dashboard's "Restart Shijhon" stopped. Not 0: a
# supervisor that starts a service again only after a failure does so too. (1: a command
# failed; 2: the configuration is invalid; 3: the start failed.)
RESTART_STATUS = 75
# Seconds the requests under way get to end when the dashboard restarts Shijhon (a song
# that is playing is cut off then; new placeholders being written were waited for before).
RESTART_GRACE = 5
# Seconds the stop for a restart may take in all, from when the server is told to stop.
# After them the process ends as it is, with the same status: a kill, and what it leaves
# is put right by the next start.
RESTART_BOUND = 60.0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="shijhon")
    parser.add_argument("--config", type=Path, help="TOML configuration file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the proxy")
    matches = sub.add_parser(
        "matches", help="list the album matches: the dry run's plans and the review list"
    )
    matches.add_argument(
        "--database", type=Path, help="the database (default: shijhon.sqlite3 in state_dir)"
    )
    matches.add_argument(
        "--all", action="store_true", help="also list complete albums and those without a match"
    )
    undo = sub.add_parser(
        "fills-undo",
        help="list (or undo) automatic fills the fill policy would not make now",
    )
    undo.add_argument(
        "--usage-export",
        type=Path,
        help="the usage export (default: [navidrome] usage_export_path; read only)",
    )
    undo.add_argument(
        "--navidrome-db",
        type=Path,
        help="or Navidrome's navidrome.db itself (default: [navidrome] database_path)",
    )
    undo.add_argument(
        "--database",
        type=Path,
        help="Shijhon's database (default: shijhon.sqlite3 in state_dir)",
    )
    undo.add_argument(
        "--expect",
        metavar="DIGEST",
        help="with --apply: only if the list still has the digest the dry run printed",
    )
    undo.add_argument(
        "--apply",
        action="store_true",
        help="undo them (only with Shijhon stopped: refused while it runs); else list only",
    )
    unused = sub.add_parser(
        "cleanup",
        help="list the releases the cleanup would take out now, and those kept (read only)",
    )
    unused.add_argument(
        "--usage-export",
        type=Path,
        help="the usage export (default: [navidrome] usage_export_path; read only)",
    )
    unused.add_argument(
        "--navidrome-db",
        type=Path,
        help="or Navidrome's navidrome.db itself (default: [navidrome] database_path)",
    )
    unused.add_argument(
        "--database",
        type=Path,
        help="Shijhon's database (default: shijhon.sqlite3 in state_dir)",
    )
    saved = sub.add_parser(
        "saved", help="list the settings saved in the dashboard (secrets hidden), or clear them"
    )
    saved.add_argument(
        "--database", type=Path, help="the database (default: shijhon.sqlite3 in state_dir)"
    )
    saved.add_argument(
        "--clear",
        metavar="SECTION[.KEY]",
        action="append",
        help="remove saved values (the configuration's apply after a restart); repeatable",
    )
    sub.add_parser("catalogs", help="list the installed catalog adapters")
    exported = sub.add_parser(
        "usage-export",
        help="write the usage export: what the cleanup reads of Navidrome's database (it"
        " reads no configuration)",
    )
    exported.add_argument(
        "--navidrome-db", type=Path, required=True, help="Navidrome's navidrome.db (read only)"
    )
    exported.add_argument("--out", type=Path, required=True, help="the export file to write")
    exported.add_argument(
        "--every",
        type=float,
        metavar="SECONDS",
        help="keep running: export now, every SECONDS, and whenever Shijhon asks"
        " (default: one export)",
    )
    sub.add_parser("version", help="print the version")
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return
    if args.command == "catalogs":
        print(_catalogs())
        return
    if args.command == "usage-export":  # (reads no configuration: it holds secrets)
        if args.every is not None and args.every <= 0:
            parser.error("--every must be more than 0")
        status = export.run(
            args.navidrome_db, args.out, every=args.every or 0.0, once=args.every is None
        )
        if status:
            raise SystemExit(status)
        return
    if args.command == "matches":
        configured = _settings(args.config)
        database = args.database or configured.database_path
        try:
            # What would be filled: as the fill policy that applies decides.
            fill = effective_from_database(configured, database).fill
            policy: FillPolicy | None = FillPolicy(fill.auto_min_songs, fill.auto_min_share)
        except sqlite3.Error:
            policy = None  # the report says what is wrong with the database
        try:
            print(report(database, everything=args.all, policy=policy))
        except ReportError as exc:
            print(f"shijhon: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        return

    if args.command == "fills-undo":
        _fills_undo(args)
        return

    if args.command == "cleanup":
        _cleanup(args)
        return

    if args.command == "saved":
        database = args.database or _settings(args.config).database_path
        try:
            if args.clear:
                print(f"removed: {clear_saved(database, args.clear)} saved value(s)")
            else:
                print(list_saved(database))
        except (OSError, sqlite3.Error) as exc:
            print(f"shijhon: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        return

    settings = _settings(args.config)
    configure_logging(settings.log_level, settings.log_debug)
    serve(settings, config=args.config)


class Restart:
    """The dashboard's "Restart Shijhon" (``ShijhonApp.restarter``): the server is told to
    stop as a stop signal tells it - no new connections, the application closed in order -
    with a few seconds for the requests under way (a song that is playing is cut off
    then). ``serve`` then exits with ``RESTART_STATUS``, and whatever runs Shijhon starts
    it again. A stop that does not end within ``RESTART_BOUND`` is ended by a timer: the
    process exits as it is. A stop signal is a stop, before and after: no restart is
    begun while the server is stopping, and neither the status nor the timer is a
    restart's once a stop signal came."""

    def __init__(self, server: uvicorn.Server) -> None:
        self.server = server
        self.requested = False

    def stopping(self) -> bool:
        """Whether the server has been told to stop already."""
        return bool(self.server.should_exit)

    def stopped(self) -> bool:
        """Whether a stop signal came (the server's own note of the signals it took)."""
        return bool(getattr(self.server, "_captured_signals", ()))

    def __call__(self) -> bool:
        """Tell the server to stop for a restart. False: it is stopping already, and
        nothing is begun."""
        if self.stopping():
            return False
        bound = Timer(RESTART_BOUND, self._overdue)
        bound.daemon = True  # (it never keeps the process from ending)
        bound.start()  # (first: where no thread can be started, nothing has been begun)
        self.requested = True
        # Only this stop is bounded here (a stop signal's has its supervisor's own bound).
        self.server.config.timeout_graceful_shutdown = RESTART_GRACE
        self.server.should_exit = True
        return True

    def _overdue(self) -> None:
        """The stop did not end in time: the process ends as it is - unless a stop signal
        came meanwhile: that stop is bounded by whoever sent it."""
        if not self.stopped():
            os._exit(RESTART_STATUS)


def started_again(settings: Settings, pid: int | None = None) -> bool:
    """Whether Shijhon is taken to be started again when it exits - only then is the
    restart offered: it runs as the first process of a container (whose restart policy is
    to do it: the shipped Compose file has one; without one the container stays stopped),
    or the configuration says that a supervisor does (``[server]
    restart_by_supervisor``)."""
    return (os.getpid() if pid is None else pid) == 1 or settings.server.restart_by_supervisor


def restart_problem(config: Path | None, running: Settings) -> str | None:
    """Why a restart would not bring Shijhon back where it is - asked before anything is
    stopped. The configuration is read as the next start reads it (the file as it is now,
    the same environment); None: nothing in it stands against the start. The reason is
    shown on the dashboard and logged: it names the settings, never what they hold."""
    try:
        fresh = load_settings(config)
    except ValidationError as exc:
        return "the configuration has an error - " + "; ".join(_problems(exc, own=False))
    except (OSError, ValueError) as exc:  # (a file that is not TOML is a ValueError)
        return f"the configuration file cannot be read ({type(exc).__name__})"
    if fresh.navidrome.library_path is None:
        return "the configuration names no library folder (navidrome.library_path)"
    was, now = running.server, fresh.server
    if (was.host, was.port) != (now.host, now.port):
        return (
            "the configuration now names another address or port for Shijhon than the one"
            " it answers at ([server] host, port)"
        )
    return None


def final_status(
    *,
    started: bool,
    should_reload: bool,
    workers: int,
    restart_requested: bool,
    stop_captured: bool,
) -> int | None:
    """The status ``serve`` exits with once the server has returned; None: it returns as
    ``uvicorn.run`` does. 3 where ``uvicorn.run`` exits with 3 - the server never started
    (said in the log), under exactly its condition: not reloading, and one worker (an
    interrupt before the start with ``WEB_CONCURRENCY`` 0 or below ends quietly there, so
    here too). ``RESTART_STATUS`` after the dashboard's restart - unless a stop signal
    came meanwhile: that is a stop (as the first process of a container, where a signal's
    default action does not end the process, ``serve`` just returns)."""
    if not started and not should_reload and workers == 1:
        return 3
    if restart_requested and not stop_captured:
        return RESTART_STATUS
    return None


def serve(settings: Settings, *, config: Path | None = None) -> None:
    """Run the proxy until it is stopped, as ``uvicorn.run`` does. ``config``: the
    configuration file the settings were read from (read again before a restart)."""
    app = create_app(settings)
    configured = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
        access_log=False,  # it would print credentials; Shijhon logs its own access lines
        server_header=False,
        date_header=False,  # Navidrome's Date header is passed through
        # The client's address is Shijhon's own reading of forwarding headers, from
        # [server] trusted_proxies only (proxy/forwarding.py): not uvicorn's.
        proxy_headers=False,
        # A failure of the app's lifespan (startup, background work) is an error in the log,
        # not "lifespan unsupported" at INFO with the background work silently gone.
        lifespan="on",
    )
    if configured.reload or configured.workers > 1:  # (WEB_CONCURRENCY: as uvicorn.run)
        logging.getLogger("uvicorn.error").warning(
            "You must pass the application as an import string to enable 'reload' or 'workers'."
        )
        raise SystemExit(3)
    server = uvicorn.Server(configured)
    restart = Restart(server)
    if started_again(settings):
        app.restarter = restart
        app.stopping = restart.stopping
        app.restart_check = lambda: restart_problem(config, settings)
    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    status = final_status(
        started=server.started,
        should_reload=configured.should_reload,
        workers=configured.workers,
        restart_requested=restart.requested,
        stop_captured=restart.stopped(),
    )
    if status == RESTART_STATUS:
        log.info(
            "restart: exiting with status %d at the dashboard's request; whatever runs"
            " Shijhon starts it again",
            RESTART_STATUS,
        )
    if status is not None:
        raise SystemExit(status)


def _catalogs() -> str:
    """The catalog adapters this installation has (``[catalog] kind``), one a line."""
    found = plugin.installed()
    lines = [f"{kind}: {adapter.label}" for kind, adapter in sorted(found.items())]
    lines += [f"{kind}: could not be loaded ({why})" for kind, why in sorted(plugin.broken())]
    return "\n".join(lines) or "no catalog adapter is installed"


def _usage_source(args: argparse.Namespace, configured: Settings) -> UsageSource:
    """Where a command reads who uses what: as given, else as configured."""
    if args.usage_export is not None and args.navidrome_db is not None:
        print("shijhon: --usage-export or --navidrome-db, not both", file=sys.stderr)
        raise SystemExit(2)
    if args.usage_export is not None or args.navidrome_db is not None:
        source = usage_configured(args.usage_export, args.navidrome_db)
    else:
        nd = configured.navidrome
        source = usage_configured(nd.usage_export_path, nd.database_path)
    if source is None:
        print(
            "shijhon: --usage-export (or [navidrome] usage_export_path), or --navidrome-db"
            " (or [navidrome] database_path), is needed",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return source


def _snapshot_line(source: UsageSource, snapshot: Snapshot) -> str:
    if not source.export:
        return ""
    made = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snapshot.at))
    return f"(who uses what: the usage export of {made})"


def _cleanup(args: argparse.Namespace) -> None:
    """The cleanup's dry run, whatever its mode: from both databases, read only (also while
    Shijhon runs). From the usage export: the one there now."""
    configured = _settings(args.config)
    database = args.database or configured.database_path
    source = _usage_source(args, configured)
    try:
        settings = effective_from_database(configured, database)
        shijhon = open_read_only(database)
    except (OSError, sqlite3.Error) as exc:
        print(f"shijhon: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    try:
        navidrome, snapshot = source.open()
    except (OSError, sqlite3.Error) as exc:
        shijhon.close()
        print(f"shijhon: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    rules = settings.cleanup
    try:
        found = cleanup.listing(
            shijhon,
            navidrome,
            now=time.time(),
            unused_days=rules.unused_days,
            catalog_albums=rules.catalog_albums,
            # Fills are taken out only where Shijhon fills albums (their albums' matches).
            fills=rules.fills and settings.fill.enabled and settings.catalog.kind != "none",
        )
    except sqlite3.Error as exc:
        print(f"shijhon: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        shijhon.close()
        navidrome.close()
    print(cleanup.report(found, mode=rules.mode, unused_days=rules.unused_days))
    if line := _snapshot_line(source, snapshot):
        print(line)
    if found.refused:
        raise SystemExit(1)


def _fills_undo(args: argparse.Namespace) -> None:
    configured = _settings(args.config)
    database = args.database or configured.database_path
    if args.expect is not None and not args.apply:
        print("shijhon: --expect goes with --apply", file=sys.stderr)
        raise SystemExit(2)
    if args.apply and database.resolve() != configured.database_path.resolve():
        # It writes the configured library: only by the records of that library's own
        # database, whose lock the running service holds (a copy has neither).
        print(
            "shijhon: --apply works on the configured state's database only"
            f" ({configured.database_path}), not on --database {database}: nothing done",
            file=sys.stderr,
        )
        raise SystemExit(2)
    # One process writes the library at a time: never beside the running service,
    # whose lock is taken before the list is made and held until the last removal.
    writer = WriterLock(database) if args.apply else None
    try:
        if writer is not None:
            writer.acquire()
    except AlreadyRunning:
        print(
            "shijhon: Shijhon (or another fills-undo --apply) is running on this database:"
            " stop it first (--apply writes the library, and must not do so beside the"
            " service); nothing done",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except OSError as exc:
        print(
            f"shijhon: cannot tell whether Shijhon is running ({exc.strerror or exc}):"
            " nothing done",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    try:
        _undo_fills(args, configured, database)
    finally:
        if writer is not None:
            writer.release()


def _undo_fills(args: argparse.Namespace, configured: Settings, database: Path) -> None:
    # The fill policy as it applies: with the values saved in the dashboard.
    try:
        settings = effective_from_database(configured, database)
    except sqlite3.Error as exc:
        print(f"shijhon: {database}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    policy = FillPolicy(settings.fill.auto_min_songs, settings.fill.auto_min_share)
    source = _usage_source(args, configured)
    try:
        items, listed = undo.plan_from(database, source, policy)
    except (OSError, sqlite3.Error) as exc:
        print(f"shijhon: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(undo.report(items))
    if line := _snapshot_line(source, listed):
        print(line)
    if not args.apply:
        return
    if args.expect is not None and args.expect != undo.digest(items):
        print(
            f"shijhon: the list changed since the dry run (it is {undo.digest(items)},"
            f" not {args.expect}): nothing done",
            file=sys.stderr,
        )
        raise SystemExit(1)
    configure_logging(settings.log_level, settings.log_debug)
    if source.export:
        print("(each removal waits for a usage export made after it: the exporter must run)")
    result = anyio.run(_apply_undo, settings, items, database, source, listed)
    print(
        f"undone: {result.done} album(s)"
        + (f", {result.gone} already gone" if result.gone else "")
        + (f", {len(result.failed)} not undone" if result.failed else "")
        + (f", {result.untried} not tried" if result.untried else "")
    )
    for line in result.failed:
        print(f"  not undone: {line}")
    if result.stopped:
        print(f"  nothing more was tried: {result.stopped}")
    if result.failed or result.stopped:
        raise SystemExit(1)


async def _apply_undo(
    settings: Settings,
    items: list[undo.Undo],
    database: Path,
    navidrome_db: UsageSource,
    listed: Snapshot | None = None,
) -> undo.Applied:
    """The engine alone (Shijhon itself is stopped: the caller holds its lock): files,
    targeted scans, records - in the database the list was made from."""
    if settings.navidrome.library_path is None:
        raise SystemExit("navidrome.library_path must point at the library root")
    store = await Store.open(database)
    navidrome = NavidromeService(
        settings.navidrome.url,
        settings.navidrome.user,
        settings.navidrome.service_password(),
        client_name=settings.navidrome.client_name,
        library_id=settings.navidrome.library_id,
        timeout=settings.navidrome.timeout_seconds,
    )
    engine = PlaceholderEngine(
        layout=Layout(settings.navidrome.library_path, settings.placeholders.folder),
        navidrome=navidrome,
        scans=ScanCoordinator(navidrome),
        silence=SilenceMaker(),
        store=store,
        # Nothing taken out unless Navidrome keeps missing files.
        writes_refused=PlaceholderWrites(
            navidrome, stated=settings.navidrome.purge_missing
        ).refused,
    )
    try:
        # What a stop of the service left half written is seen (and put right before its
        # release is written), as at the service's own start.
        await engine.load_pending()
        return await undo.apply(items, engine, store, navidrome_db, listed=listed)
    finally:
        await navidrome.aclose()
        await store.close()


def _settings(config: Path | None) -> Settings:
    try:
        return load_settings(config)
    except ValidationError as exc:
        for problem in _problems(exc):
            if problem.startswith("in "):
                print(f"shijhon: configuration error {problem}", file=sys.stderr)
            else:
                print(f"shijhon: configuration error: {problem}", file=sys.stderr)
        raise SystemExit(2) from None


def _problems(exc: ValidationError, *, own: bool = True) -> list[str]:
    """One line per problem of the configuration: "in <where>: <what>", or what alone.
    ``own`` off: nothing a configuration could hold - not the words of Shijhon's own
    checks, which may quote a value, and of where only the section and the setting (below
    them a name can be the configuration's own: a key of a table), or the section alone
    for a setting that does not exist."""
    lines = []
    for error in exc.errors(include_input=False):
        parts = [str(part) for part in error["loc"]]
        where = ".".join(parts if own else parts[:2])
        message = str(error["msg"]).removeprefix("Value error, ")
        if not own and error["type"] in ("value_error", "assertion_error"):
            message = "not accepted (a start says why)"
        if not own and error["type"] == "extra_forbidden":  # (the name is the file's own)
            where, message = ".".join(parts[:-1][:1]), "a setting that does not exist"
        if line := f"in {where}: {message}" if where else message:
            lines.append(line)
    return lines if own else list(dict.fromkeys(lines))
