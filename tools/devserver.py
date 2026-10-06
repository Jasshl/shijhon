"""A development instance for checks with real apps.

Starts the pinned Navidrome (loopback only) with a persistent state folder, writes
synthetic owned tracks where the configuration asks for them, starts Shijhon on the
configured address, registers the configured add-ons and materializes the configured
releases as placeholders. Requests are logged (without credentials) to
``<state>/requests.log`` as evidence.

    uv run python tools/devserver.py --config /path/outside/the/repo/devserver.toml

State (Navidrome database, placeholders, Shijhon's database) is kept in ``--state`` so
favorites and playlists survive restarts; ``--reset`` starts over.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import NavidromeInstance
from tests.harness.running import RunningServer

from shijhon.app import Services, ShijhonApp
from shijhon.catalog.model import CatalogRef
from shijhon.config import load_settings
from shijhon.log import RedactingFormatter, configure_logging
from shijhon.views.commits import CommitError
from shijhon.views.ids import CatalogId
from tools.devconfig import DevConfig, ReleaseConfig, load

log = logging.getLogger("devserver")


def owned_album(item: ReleaseConfig) -> Album | None:
    if not item.owned:
        return None
    release = item.release
    tracks = tuple(
        Track(t.title, t.number, disc=t.disc, seconds=t.duration_ms / 1000, isrc=t.isrc)
        for t in release.tracks
        if t.disc == 1 and t.number in item.owned
    )
    return Album(
        release.artist,
        release.title,
        tracks,
        recording_date=release.release_date,
        folder=f"Owned/{release.artist}/{release.title}",
    )


def debug_loggers(config: DevConfig) -> list[str]:
    """Loggers at debug level: the configuration's ``log_debug``, else the environment's
    (``SHIJHON_LOG_DEBUG``) - as the instance's settings have them."""
    return config.log_debug if config.log_debug is not None else load_settings(None).log_debug


def start(config: DevConfig, state: Path) -> tuple[NavidromeInstance, RunningServer, ShijhonApp]:
    nd = NavidromeInstance(state / "navidrome", admin=(config.user, config.password))
    started: list[RunningServer] = []
    try:
        server, app = _start(config, nd, state, started)
    except BaseException:
        # Navidrome runs in its own session: stop it, or it outlives a failed start.
        for running in started:
            running.stop()
        nd.stop()
        raise
    return nd, server, app


def _start(
    config: DevConfig, nd: NavidromeInstance, state: Path, started: list[RunningServer]
) -> tuple[RunningServer, ShijhonApp]:
    first_run = not nd.data.exists()
    for item in config.releases:
        album = owned_album(item)
        if album is not None and not (nd.music / album.relative_folder).exists():
            write_album(nd.music, album)
    nd.start()
    if first_run:
        nd.create_admin()
    nd.scan(full=first_run)

    settings = load_settings(
        None,
        state_dir=state / "shijhon",
        server={"host": config.host, "port": config.port},
        delivery=config.delivery,
        catalog=config.catalog,
        search=config.search,
        fill=config.fill,
        cleanup=config.cleanup,
        log_debug=debug_loggers(config),
        navidrome={
            "url": nd.base_url,
            "user": config.user,
            "password": config.password,
            "library_path": nd.music,
            # Read only: whether anyone uses a release (the cleanup of unused ones).
            "database_path": nd.data / "navidrome.db",
        },
        # Synchronized at startup: added, updated, ordered; removed ones disabled.
        addons=[
            {
                "name": addon.name,
                "base_url": addon.base_url,
                "reach": addon.reach.value,
                "settings": addon.settings,
                "budget_seconds": addon.budget_seconds,
            }
            for addon in config.addons
        ],
    )
    app = ShijhonApp(settings)
    server = RunningServer(app, host=config.host, port=config.port)
    server.start()
    started.append(server)
    assert app.services is not None
    services = app.services

    for item in config.releases:
        release = item.release
        if not item.fill:
            continue  # owned files only: matched and filled by Shijhon when shown or opened
        owned_id = None
        links: dict[CatalogRef, str] = {}
        if item.owned:
            found = nd.client().ok(
                "search3", {"query": release.title, "albumCount": 20, "songCount": 0}
            )
            albums = [
                a for a in found["searchResult3"].get("album", []) if a["name"] == release.title
            ]
            if albums:
                owned_id = albums[0]["id"]
                songs = server.call(lambda i=owned_id: services.engine.owned_album(i)).songs
                by_position = {(s["discNumber"], s["trackNumber"]): s["id"] for s in songs}
                links = {
                    t.ref: by_position[(t.disc, t.number)]
                    for t in release.tracks
                    if (t.disc, t.number) in by_position
                }
        result = server.call(
            lambda r=release, o=owned_id, lk=links: services.engine.materialize(
                r, owned_album_id=o, links=lk
            ),
            timeout=300,
        )
        log.info(
            "%s — %s: %d placeholder(s), %d owned", release.artist, release.title,
            len(result.created), len(result.linked),
        )  # fmt: skip
    commit_albums(config, services, server)
    return server, app


def commit_albums(config: DevConfig, services: Services, server: RunningServer) -> None:
    """Add the configured catalog albums, as a client's commit would."""
    commits, catalog = services.commits, services.catalog
    if not config.commit_albums:
        return
    if commits is None or catalog is None:
        log.warning("[commits] albums need a [catalog] section; skipped")
        return
    for album in config.commit_albums:
        cid = CatalogId("al", CatalogRef(catalog.key, album))
        try:
            native = server.call(lambda c=cid: commits.native(c), timeout=300)
        except CommitError as exc:
            log.warning("catalog album %s not added: %s", album, exc.message)
            continue
        log.info("catalog album %s is library album %s", album, native)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("var/devserver"))
    parser.add_argument("--reset", action="store_true", help="delete the state folder first")
    args = parser.parse_args()

    config = load(args.config)
    configure_logging("INFO", debug_loggers(config))
    args.state = args.state.resolve()  # Navidrome runs with its own working directory
    if args.reset:
        shutil.rmtree(args.state, ignore_errors=True)
    args.state.mkdir(parents=True, exist_ok=True)
    evidence = logging.FileHandler(args.state / "requests.log")
    evidence.setFormatter(RedactingFormatter("%(asctime)s %(message)s"))
    logging.getLogger("shijhon.access").addHandler(evidence)

    # Whatever ends this tool, its Navidrome ends too: a signal (also a closed terminal,
    # unless hangups are ignored: nohup) or an error stops it here, before the tool exits;
    # if the tool is killed outright, the harness's tether does (a Navidrome left behind
    # would keep the instance's database open next to the next one).
    signals = [signal.SIGTERM]
    if signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:
        signals.append(signal.SIGHUP)

    def leave(*_: object) -> None:
        raise SystemExit(1)  # during the start: it stops what it started

    for number in signals:
        signal.signal(number, leave)
    started = None
    try:
        started = start(config, args.state)
        stop = threading.Event()
        for number in (signal.SIGINT, *signals):
            signal.signal(number, lambda *_: stop.set())
        log.info("Shijhon is serving clients at %s (user %s)", started[1].base_url, config.user)
        stop.wait()
    finally:
        if started is not None:
            try:
                started[1].stop()
            finally:
                started[0].stop()


if __name__ == "__main__":
    main()
