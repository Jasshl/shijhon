"""A library write in a process of its own, killed with SIGKILL at one of its steps: what a
redeploy's kill, an OOM kill or a power loss leaves behind (suite R). No handler, no
``finally`` and no shielded compensation runs - as with the real thing.

Run as ``python -m tests.harness.killed '<spec as JSON>'``; the spec names Navidrome's
address, the library, Shijhon's database, the step to die at and the write:
``{"action": "materialize", "key": ..., "count": ..., "cover": bool, "owned": <album ID
to fill, its songs linked by track number>, "only": [track numbers]}``,
``{"action": "restore", "ref": <a release taken out>}``,
``{"action": "change", "album": <a filled album>, "title": ..., "from": [key, songs],
"to": [key, songs], "navidrome_db": <Navidrome's database>}`` (the album filled from
another edition, ``tests.harness.engine.edition_with``, through the cleanup's own removal),
``{"action": "replace", "song": ..., "delivered": <path>}``,
``{"action": "revert", "song": ...}``, ``{"action": "recover", "song": ...}`` (the
startup's repair of a swap a stop interrupted), ``{"action": "retag", "song": ..., "key": ...,
"count": ..., "title": <the new title>}``, ``{"action": "track_lists", "key": ..., "count":
...}`` (the library pass reading the release's track list again, as ``listed_later`` lists
it) or ``{"action": "artist_list", "lists": {text:
[names]}}`` (an artist lists pass over the library's files; its steps are "write <step>").
The step "between renames" kills a swap after its first rename (the old file is in its
backup, the new one not yet in place). Exits with 3 when the step was never reached.
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import anyio

from shijhon.cleanup import Cleanup
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.navidrome.usage import UsageSource
from shijhon.placeholders.engine import PlaceholderEngine
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.silence import SilenceMaker
from shijhon.store import Store
from tests.harness.engine import (
    PLACEHOLDER_FOLDER,
    catalog_release,
    edition_with,
    fills_for,
    listed_later,
    track_lists_for,
)
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

NOT_REACHED = 3


async def run(spec: dict[str, Any]) -> None:
    if spec["action"] in ("import_write", "import_undo"):
        await _import_list(spec)
        return
    service = NavidromeService(spec["url"], ADMIN_USER, ADMIN_PASSWORD, client_name="shijhon")
    store = await Store.open(Path(spec["database"]))
    engine = PlaceholderEngine(
        layout=Layout(Path(spec["music"]), PLACEHOLDER_FOLDER),
        navidrome=service,
        scans=ScanCoordinator(service),
        silence=SilenceMaker(),
        store=store,
    )

    async def step(name: str) -> None:
        if name == spec["kill_at"]:
            os.kill(os.getpid(), signal.SIGKILL)

    engine.step = step
    if spec["action"] == "artist_list":
        await _artist_list(spec, service, store, step)
        return
    if spec["kill_at"] == "between renames":
        rename = os.replace

        def replace(src: Any, dst: Any) -> None:
            rename(src, dst)
            if Path(dst).name.startswith("backup-"):
                os.kill(os.getpid(), signal.SIGKILL)

        os.replace = replace  # type: ignore[assignment]
    if spec["kill_at"] == "placeholder linked":
        link = os.link

        def linked(src: Any, dst: Any, **kwargs: Any) -> None:
            link(src, dst, **kwargs)
            if Path(src).parent == engine.layout.staging:
                os.kill(os.getpid(), signal.SIGKILL)

        os.link = linked  # type: ignore[assignment]
    action = spec["action"]
    if action in ("materialize", "retag", "track_lists"):
        key = spec["key"]
        release = catalog_release(key, f"Album {key}", f"Artist {key}", spec["count"])
    if action == "materialize" and spec.get("owned"):
        owned = await engine.owned_album(spec["owned"])
        numbers = {s["trackNumber"]: s["id"] for s in owned.songs}
        links = {t.ref: numbers[t.number] for t in release.tracks if t.number in numbers}
        await engine.materialize(release, owned_album_id=spec["owned"], links=links)
    elif action == "materialize":
        cover = b"\xff\xd8\xff\xe0 cover" if spec.get("cover") else None
        numbers = spec.get("only")
        only = [t.ref for t in release.tracks if t.number in numbers] if numbers else None
        await engine.materialize(release, cover=cover, only=only)
    elif action == "restore":
        await engine.restore_release(spec["ref"])
    elif action == "change":
        editions = [
            edition_with(key, spec["title"], {int(n): at for n, at in songs.items()})
            for key, songs in (spec["from"], spec["to"])
        ]
        fills = fills_for(engine, store, service, *editions)
        usage = UsageSource(Path(spec["navidrome_db"]), export=False)
        cleanup = Cleanup(engine, database=Path(spec["database"]), usage=usage, filler=fills)
        await fills.change(spec["album"], str(editions[1].ref), cleanup.take_out_now)
    elif action == "replace":
        await engine.replace_with_delivered(spec["song"], Path(spec["delivered"]))
    elif action == "revert":
        await engine.revert_to_placeholder(spec["song"])
    elif action == "redeliver":
        await engine.redeliver(spec["song"], Path(spec["delivered"]), unused_since=float("inf"))
    elif action == "recover":
        await engine.recover_swap(spec["song"])
    elif action == "retag":
        track = dataclasses.replace(release.tracks[0], title=spec["title"])
        await engine.retag(spec["song"], track, release)
    elif action == "track_lists":
        lists = await track_lists_for(engine, store, service, listed_later(release))
        await lists.run(dry_run=False, run_seconds=6 * 3600)
    else:
        raise ValueError(action)


async def _artist_list(spec: dict[str, Any], service: Any, store: Any, step: Any) -> None:
    from shijhon.artist_lists.files import LibraryFiles
    from shijhon.artist_lists.names import listed
    from shijhon.artist_lists.repair import ArtistLists
    from shijhon.artist_lists.sources import Resolution, Subject

    lists = spec["lists"]

    class Listing:
        async def resolve(
            self, subject: Subject, *, link: bool = True, skips: Any = None
        ) -> Resolution:
            spelled = listed(subject.artist, lists.get(subject.artist) or [])
            return (
                Resolution("resolved", tuple(spelled), "link")
                if spelled
                else Resolution("unresolved")
            )

    files = LibraryFiles(Path(spec["music"]), store, service, ScanCoordinator(service))
    files.step = step
    worker = ArtistLists(
        service, store, Listing(), files, placeholder_folder=PLACEHOLDER_FOLDER, report=False,  # type: ignore[arg-type]
        library=True, pause=0,
    )  # fmt: skip
    await worker.once()


def import_login(url: str) -> Any:
    import httpx

    def login(name: str, password: str) -> str:
        answer = httpx.post(
            f"{url}/auth/login", json={"username": name, "password": password}, timeout=10
        )
        answer.raise_for_status()
        return str(answer.json()["token"])

    return login


async def _import_list(spec: dict[str, Any]) -> None:
    """An import file's artist list given (``import_write``: ``song``, ``names``,
    ``artist``) or taken back (``import_undo``), killed at ``kill_at``."""
    from tests.harness.import_world import open_world

    world = await open_world(
        spec["url"],
        Path(spec["music"]),
        Path(spec["database"]),
        tuple(spec["users"]),
        login=import_login(spec["url"]),
    )

    async def step(name: str) -> None:
        if name == spec["kill_at"]:
            os.kill(os.getpid(), signal.SIGKILL)

    world.lists.step = step
    try:
        await _import_listed(spec, world)
    finally:
        await world.close()  # (not killed: the step was not reached)


async def _import_listed(spec: dict[str, Any], world: Any) -> None:

    def shows(names: list[str]) -> Any:
        def settled(song: Any, size: int) -> bool:
            people = [p["name"] for p in (song or {}).get("participants", {}).get("artist", [])]
            return bool(song) and song.get("size") == size and people == names

        return settled

    if spec["action"] == "import_write":
        request = {"values": {"artists": spec["names"]}, "pin": True, "artist": spec["artist"]}
        await world.lists.write(
            spec["song"], request, shows(spec["names"]), {"artist": spec["artist"]}
        )
    else:
        row = await world.store.fetchone("SELECT * FROM artist_lists WHERE state = 'written'")
        await world.lists.undo(dict(row), shows([spec["artist"]]))


def main() -> None:
    anyio.run(run, json.loads(sys.argv[1]))
    sys.exit(NOT_REACHED)


if __name__ == "__main__":
    main()
