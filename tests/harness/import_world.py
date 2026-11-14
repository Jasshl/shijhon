"""Music imports against a real test Navidrome, opened and opened again on the same state
(an import's artist lists, suite R's stops of them): the import service, its artist lists
and the accounts' native sign-ins. Accounts are created at the first opening only."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from shijhon.collection.origins import Origins
from shijhon.collection.reader import CallerLibrary
from shijhon.collection.service import Collection
from shijhon.music_imports import ImportLimits, ManifestEntry, MusicImports
from shijhon.music_imports.lists import ImportLists
from shijhon.navidrome.checks import PlaceholderWrites
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.store import Store

PASSWORD = "invented-password-"


@dataclass
class ImportWorld:
    store: Store
    native: NavidromeService
    scans: ScanCoordinator
    imports: MusicImports
    lists: ImportLists
    users: dict[str, str]  # name -> Navidrome user ID

    async def close(self) -> None:
        await self.imports.aclose()
        await self.store.close()
        await self.native.aclose()


async def open_world(
    url: str,
    music: Path,
    database: Path,
    names: tuple[str, ...],
    *,
    create: Any = None,
    login: Any = None,
) -> ImportWorld:
    """``create(name, password) -> user ID`` makes the accounts (the first opening);
    ``login(name, password) -> token`` signs an account in."""
    from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

    native = NavidromeService(url, ADMIN_USER, ADMIN_PASSWORD, client_name="shijhon")
    caller = CallerLibrary(SimpleNamespace(client=SimpleNamespace(native=native)))
    users = {}
    for name in names:
        if create is not None:
            create(name, PASSWORD + name)
        token = login(name, PASSWORD + name)
        principal = await caller.access.verify_native(token)
        caller.remember(principal, token)
        users[name] = principal.user_id
    store = await Store.open(database)
    scans = ScanCoordinator(native)
    collection = Collection(store, caller)
    (music / "_imports").mkdir(exist_ok=True)
    writes = PlaceholderWrites(native)

    async def gate() -> None:
        assert await writes.refused(fresh=True) is None

    imports = MusicImports(
        store,
        caller,
        library_id="1",
        library_path=music,
        limits=replace(ImportLimits(), min_free_bytes=0),
        scan=scans.scan,
        native_lookup=caller.imported_file,
        refresh=collection.refresh,
        native_song=caller.song,
        write_gate=gate,
    )
    collection.index.asset_lookup = imports.asset_lookup
    await Origins(store).initialize_personal("1")
    await imports.initialize()
    lists = ImportLists(imports, native)
    imports.lists = lists
    if create is not None:
        for ident in users.values():
            await collection.refresh(ident, "1")
    return ImportWorld(store, native, scans, imports, lists, users)


async def published(w: ImportWorld, owner: str, files: list[Path], root: Path, key: str) -> None:
    """Upload, check and approve ``files``; ticks until the import is done."""
    job = await w.imports.create(owner, key)
    entries = [
        ManifestEntry(f"f{n}", str(f.relative_to(root)), f.stat().st_size)
        for n, f in enumerate(files)
    ]
    job = await w.imports.declare(owner, job["id"], "page", entries)
    await w.imports.seal(owner, job["id"])
    by_key = {f"f{n}": f for n, f in enumerate(files)}
    for item in job["items"]:
        data = by_key[item["client_key"]].read_bytes()

        async def stream(data: bytes = data) -> Any:
            yield data

        await w.imports.receive(owner, job["id"], item["id"], 0, stream())
    await w.imports.review(owner, job["id"])
    status = await _settle(w, owner, job["id"], "review")
    await w.imports.approve(owner, job["id"], status["revision"], "go-" + job["id"])
    await _settle(w, owner, job["id"], "done")


async def _settle(w: ImportWorld, owner: str, job_id: str, state: str) -> dict[str, Any]:
    for _ in range(200):
        status = await w.imports.status(owner, job_id, limit=500)
        if status["state"] == state:
            return status
        if status["state"] in ("paused", "cancelled"):
            raise AssertionError(status)
        assert await w.imports.tick(), status
    raise AssertionError(f"the import did not reach {state}")
