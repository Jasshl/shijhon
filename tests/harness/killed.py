"""A library write in a process of its own, killed with SIGKILL at one of its steps: what a
redeploy's kill, an OOM kill or a power loss leaves behind (suite R). No handler, no
``finally`` and no shielded compensation runs - as with the real thing.

Run as ``python -m tests.harness.killed '<spec as JSON>'``; the spec names Navidrome's
address, the library, Shijhon's database, the step to die at and the write:
``{"action": "materialize", "key": ..., "count": ..., "cover": bool, "owned": <album ID
to fill, its songs linked by track number>, "only": [track numbers]}``,
``{"action": "restore", "ref": <a release taken out>}``,
``{"action": "replace", "song": ..., "delivered": <path>}``,
``{"action": "revert", "song": ...}``, ``{"action": "recover", "song": ...}`` (the
startup's repair of a swap a stop interrupted) or ``{"action": "retag", "song": ..., "key": ...,
"count": ..., "title": <the new title>}``. The step "between renames" kills a swap after
its first rename (the old file is in its backup, the new one not yet in place). Exits with
3 when the step was never reached.
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

from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.placeholders.engine import PlaceholderEngine
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.silence import SilenceMaker
from shijhon.store import Store
from tests.harness.engine import PLACEHOLDER_FOLDER, catalog_release
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

NOT_REACHED = 3


async def run(spec: dict[str, Any]) -> None:
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
    if spec["kill_at"] == "between renames":
        rename = os.replace

        def replace(src: Any, dst: Any) -> None:
            rename(src, dst)
            if Path(dst).name.startswith("backup-"):
                os.kill(os.getpid(), signal.SIGKILL)

        os.replace = replace  # type: ignore[assignment]
    action = spec["action"]
    if action in ("materialize", "retag"):
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
    elif action == "replace":
        await engine.replace_with_delivered(spec["song"], Path(spec["delivered"]))
    elif action == "revert":
        await engine.revert_to_placeholder(spec["song"])
    elif action == "recover":
        await engine.recover_swap(spec["song"])
    elif action == "retag":
        track = dataclasses.replace(release.tracks[0], title=spec["title"])
        await engine.retag(spec["song"], track, release)
    else:
        raise ValueError(action)


def main() -> None:
    anyio.run(run, json.loads(sys.argv[1]))
    sys.exit(NOT_REACHED)


if __name__ == "__main__":
    main()
