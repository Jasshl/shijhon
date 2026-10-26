"""Explicit caller sign-in and source preparation for browsing acceptance fixtures."""

from __future__ import annotations

import secrets
from collections.abc import Sequence

from shijhon.collection.contracts import Query
from tests.harness.dashboard import Browser
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance
from tests.harness.running import RunningServer


def prepare_collection(
    nd: NavidromeInstance,
    server: RunningServer,
    *,
    user: str = ADMIN_USER,
    password: str = ADMIN_PASSWORD,
) -> str:
    """Use a real caller login, then await a complete snapshot through root services.

    Opt in only in fixtures whose tests need library enumeration. This neither saves
    catalog membership nor substitutes service credentials for caller source reads.
    """
    browser = Browser(server.base_url)
    try:
        assert browser.sign_in(user, password).status_code == 303
    finally:
        browser.close()
    owner = str(nd.login(user, password)["id"])
    refresh_collection(server, owner)
    return owner


def refresh_collection(server: RunningServer, owner: str) -> None:
    async def refresh() -> None:
        app = server.config.app
        assert app.services is not None
        collection = app.services.collection
        assert collection is not None
        await collection.refresh(owner, str(app.configured.navidrome.library_id))

    server.call(refresh)


def select_catalog(
    nd: NavidromeInstance,
    server: RunningServer,
    refs: Sequence[str],
    *,
    saved: bool = True,
    user: str = ADMIN_USER,
    password: str = ADMIN_PASSWORD,
) -> None:
    """Explicitly save/remove named synthetic tracks for fixture library-view checks."""
    assert refs
    owner = prepare_collection(nd, server, user=user, password=password)

    async def select() -> None:
        collection = server.config.app.services.collection
        library = str(server.config.app.configured.navidrome.library_id)
        operation = await collection.prepare(
            owner,
            library,
            Query(view="all", scope="song", origin="catalog"),
            action="save" if saved else "remove",
            selected=["catalog:" + ref for ref in refs],
            request_key="fixture-selection-" + secrets.token_hex(8),
            profile_id="all",
        )
        await collection.tick()
        frozen = await collection.operation(owner, operation)
        assert frozen["state"] == "review" and frozen["total"] == len(set(refs)), frozen
        await collection.approve(owner, operation)
        for _ in range(10):
            await collection.tick()
            changed = await collection.operation(owner, operation)
            if changed["state"] == "done":
                break
        assert changed["state"] == "done", changed

    server.call(select)
