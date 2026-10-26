"""Disposable real native website + visibility adapters; synthetic data only."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio

from shijhon.access import AccessService
from shijhon.collection.contracts import IndexState, Mode
from shijhon.collection.index import structural_metadata
from shijhon.collection.native import NativeVisibility
from shijhon.collection.origins import Origins
from shijhon.collection.policy import builtin_profile_id, default_profile_id
from shijhon.collection.query_index import DEFAULT_BUDGETS, Binding, Budgets, QueryIndex
from shijhon.collection.visibility import Visibility
from shijhon.navidrome.client import NavidromeService
from shijhon.proxy.app import ProxyApp
from shijhon.proxy.auth import CredentialChecker
from shijhon.proxy.upstream import Receive, Scope, Send, Upstream
from shijhon.store import Store
from tests.harness.navidrome import NavidromeInstance


class CollectionReadModel:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def mode(self, owner_id: str) -> Mode:
        row = await self.store.fetchone(
            "SELECT mode FROM collection_preferences WHERE owner_id=?", [owner_id]
        )
        return row["mode"] if row else "saved"

    async def index_state(self, owner_id: str, library_id: str) -> IndexState:
        row = await self.store.fetchone(
            "SELECT * FROM collection_index WHERE library_id=?", [library_id]
        )
        assert row is not None
        return IndexState(**dict(row))


class CollectionApp:
    def __init__(
        self,
        nd: NavidromeInstance,
        state: Path,
        songs: list[dict[str, Any]],
        *,
        wait: float = DEFAULT_BUDGETS.wait_seconds,
        artist_participations: bool = False,
        removed: bool = False,
    ) -> None:
        """``removed``: each account removed the songs that are not originals by hand from
        its All and its Original imported library - where the albums of its originals would
        bring them in - so that both hold the originals alone."""
        self.nd, self.state, self.songs, self.wait = nd, state, songs, wait
        self.artist_participations = artist_participations
        self.removed = removed
        self.originals = {s["id"] for i, s in enumerate(songs) if i % 3 == 0}
        self.proxy: ProxyApp
        self.store: Store
        self.index: QueryIndex
        self.loop: asyncio.AbstractEventLoop
        self.native: NativeVisibility

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self.proxy(scope, receive, send)
            return
        await receive()
        self.loop = asyncio.get_running_loop()
        self.store = await Store.open(self.state / "collection.sqlite3")
        # This fixture intentionally exercises the historical shared-original policy.
        # Production startup initializes it explicitly; the standalone adapter must too.
        await Origins(self.store).initialize_legacy("1")
        await self.store.execute(
            "INSERT INTO collection_index VALUES ('1','fixture','ready',?,1,'')", [len(self.songs)]
        )
        async with self.store.transaction() as conn:
            await conn.executemany(
                "INSERT INTO collection_tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "1",
                        "native:" + s["id"],
                        s["id"],
                        "native:" + s["albumId"],
                        s["albumId"],
                        s.get("artistId", ""),
                        s["title"],
                        s["album"],
                        s["artist"],
                        s["title"],
                        s["duration"],
                        s["size"],
                        s["year"],
                        structural_metadata(s),
                        int(s["id"] in self.originals),
                        1,
                        "fixture",
                        1,
                    )
                    for s in self.songs
                ],
            )
        # Private built-in enable rows are implementation state, not visible custom libraries.
        users = self.nd.native("GET", "user").json()
        async with self.store.transaction() as conn:
            for user in users:
                await conn.execute(
                    (
                        "INSERT INTO "
                        "collection_profiles(id,owner_id,kind,name,base_original,created)"
                        " VALUES (?,?,'builtin','Built-in enables',0,1)"
                    ),
                    [default_profile_id(user["id"]), user["id"]],
                )
                if self.removed:
                    original = builtin_profile_id(user["id"], "original")
                    await conn.execute(
                        "INSERT INTO "
                        "collection_profiles(id,owner_id,kind,name,base_original,created)"
                        " VALUES (?,?,'builtin',?,0,1)",
                        [original, user["id"], original],
                    )
                    await conn.executemany(
                        "INSERT INTO collection_membership VALUES (?,?,'1',?,0,1,'removed')",
                        [
                            (user["id"], profile, "native:" + s["id"])
                            for profile in (default_profile_id(user["id"]), original)
                            for s in self.songs
                            if s["id"] not in self.originals
                        ],
                    )
        upstream = Upstream(self.nd.base_url)
        service = NavidromeService(self.nd.base_url, self.nd.admin_user, self.nd.admin_password)
        self.proxy = ProxyApp(upstream, CredentialChecker(upstream))
        async with anyio.create_task_group() as group:
            self.index = QueryIndex(
                self.state / "private" / "queries.sqlite3",
                self.state / "collection.sqlite3",
                group.start_soon,
                budgets=Budgets(wait_seconds=self.wait),
            )
            visibility = Visibility(
                upstream,
                self.proxy.checker,
                AccessService(service),
                CollectionReadModel(self.store),
                self.index,
                artist_participations=self.artist_participations,
            )
            visibility.attach(self.proxy)
            self.native = NativeVisibility(visibility)
            self.native.attach(self.proxy)
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await self.index.close()
            group.cancel_scope.cancel()
        await service.aclose()
        await upstream.aclose()
        await self.store.close()
        await send({"type": "lifespan.shutdown.complete"})


@contextmanager
def prepared_searches(
    index: QueryIndex, delay: float = 0.0, hold: threading.Event | None = None
) -> Iterator[list[str]]:
    """Searches are prepared as query generations, as one with a full first source page
    (500 or more results) is, not answered directly; each build starts ``delay`` seconds
    late, and not before ``hold`` is set. Yields the keys of the builds started meanwhile."""
    original_build, original_ready = index._build, index.has_ready
    built: list[str] = []

    async def build(*args: Any, **kwargs: Any) -> None:
        built.append(args[0])
        await anyio.sleep(delay)
        if hold is not None:
            await anyio.to_thread.run_sync(hold.wait, abandon_on_cancel=True)
        await original_build(*args, **kwargs)

    async def ready(binding: Binding, query: object) -> bool:
        return True  # (only searches ask: no first page is read beforehand)

    index._build = build  # type: ignore[method-assign]
    index.has_ready = ready  # type: ignore[method-assign]
    try:
        yield built
    finally:
        index._build = original_build  # type: ignore[method-assign]
        index.has_ready = original_ready  # type: ignore[method-assign]
