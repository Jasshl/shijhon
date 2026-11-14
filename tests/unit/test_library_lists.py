"""The Library page's lists: one read of their own, the search inside it, and the words for
candidate releases that never hold a page up for long."""

from __future__ import annotations

import time
import unicodedata
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.dashboard import library
from shijhon.dashboard.library import PAGE, Describer, Selections, Shown, read
from shijhon.matching.normalize import fold
from shijhon.store import Store

pytestmark = pytest.mark.anyio
SCOPE = "demo.xx"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    opened = await Store.open(tmp_path / "lists.db")
    try:
        yield opened
    finally:
        await opened.close()


async def seed(store: Store, filled: int, review: int, none: int) -> None:
    rows: list[tuple[str, str, str, str]] = []
    rows += [(f"f{n:04d}", "filled", f"Filled {n}", "Ana Brandt") for n in range(filled)]
    rows += [(f"r{n:04d}", "review", f"Review {n}", "Otto Vance") for n in range(review)]
    rows += [(f"n{n:04d}", "none", f"Paper Boats {n}", "Ilse Marek") for n in range(none)]
    async with store.transaction() as conn:
        await conn.executemany(
            "INSERT INTO album_matches (album_id, scope, outcome, release_ref, title, artist,"
            " checked_at) VALUES (?, ?, ?, 'demo:1', ?, ?, 0)",
            [(a, SCOPE, outcome, title, artist) for a, outcome, title, artist in rows],
        )
        await conn.executemany(
            "INSERT INTO releases (ref, folder, album_id, owned_album_id, title, artist,"
            " album_tags, data, created_at) VALUES (?, ?, ?, ?, 't', 'a', '{}', '{}', 0)",
            [(f"demo:{a}", f"folder/{a}", f"nd-{a}", a) for a, *_ in rows if a[0] == "f"],
        )


async def test_the_lists_are_read_without_the_store_s_writer(store: Store) -> None:
    """A page's lists never wait for the store's writer (a library write, a stream's
    record): they are read in a transaction of their own."""
    await seed(store, filled=60, review=3, none=2)
    async with store._lock:  # the writer busy
        with anyio.fail_after(5):
            lists = await read(store, SCOPE, filled_page=2)
    assert [len(lists.rows[name]) for name in ("review", "none")] == [3, 2]
    # Of the many filled albums: their number and the page asked for, not every row.
    rows, total, page = lists.filled()
    assert (total, page, len(rows)) == (60, 2, PAGE)
    assert lists.rows["filled"] == []


async def test_a_filled_album_without_names_shows_its_release_s(store: Store) -> None:
    """A match recorded without the album's title and artist is listed under the names of
    the release it is filled from, not as an unknown album."""
    async with store.transaction() as conn:
        await conn.execute(
            "INSERT INTO album_matches (album_id, scope, outcome, release_ref, title, artist,"
            " checked_at) VALUES ('f1', ?, 'filled', 'demo:f1', '', '', 0)",
            [SCOPE],
        )
        await conn.execute(
            "INSERT INTO releases (ref, folder, album_id, owned_album_id, title, artist,"
            " album_tags, data, created_at) VALUES ('demo:f1', 'folder/f1', 'nd-f1', 'f1',"
            " 'Low Fences', 'Ana Brandt', '{}', '{}', 0)"
        )
    rows, total, _ = (await read(store, SCOPE, filled_page=1)).filled()
    assert total == 1 and (rows[0].title, rows[0].artist) == ("Low Fences", "Ana Brandt")


async def test_the_search_narrows_every_list_inside_the_read(store: Store) -> None:
    await seed(store, filled=30, review=2, none=4)
    lists = await read(store, SCOPE, "paper BOATS", filled_page=1)
    assert [len(lists.rows[name]) for name in ("review", "none")] == [0, 4]
    assert lists.filled_total == 0
    lists = await read(store, SCOPE, "ana", filled_page=1)
    assert (lists.filled_total, len(lists.filled()[0])) == (30, PAGE)
    assert lists.rows["none"] == []


@pytest.mark.parametrize(
    "text",
    ["Into The Quiet Harbor", "AC/DC - Live!", "  tabs\tand\nlines ", "", "123 abc_DEF", "ÉMILE"],
)
def test_the_quick_fold_is_the_same_as_the_full_one(text: str) -> None:
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    assert fold(text) == "".join(ch for ch in plain.casefold() if ch.isalnum())
    words = [w for w in (fold(part) for part in text.split()) if w]
    assert library._searched(text) == " ".join(words)


def test_kept_lists_share_the_albums_shown_alike() -> None:
    selections = Selections()
    first = {f"a{n}": Shown(f"state{n}", "demo:1") for n in range(3)}
    selections.issue("viewer", "review", first)
    again = {f"a{n}": Shown(f"state{n}", "demo:1") for n in range(3)} | {"b": Shown("s")}
    token = selections.issue("viewer", "review", again)
    kept = selections._kept[token].albums
    assert list(kept) == ["a0", "a1", "a2", "b"]
    assert all(kept[a] is first[a] for a in first)  # one entry for both lists


class SlowCatalog:
    key = "demo"

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.asked: list[str] = []

    async def album(self, item: str) -> Any:
        self.asked.append(item)
        await anyio.sleep(self.seconds)
        raise LookupError(item)  # (remembered as not described; enough here)


async def test_a_short_wait_describes_the_rest_in_the_background() -> None:
    catalog = SlowCatalog(0.3)
    describer = Describer()
    async with anyio.create_task_group() as group:

        def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> bool:
            group.start_soon(work)
            return True

        started = time.monotonic()
        await describer.describe(catalog, ["demo:1", "demo:2"], wait=0.0, spawn=spawn)
        assert time.monotonic() - started < 0.25  # not waited for
        # Another page asking meanwhile waits for the same asking, never asks again.
        await describer.describe(catalog, ["demo:1"], wait=1.0, spawn=spawn)
        assert sorted(catalog.asked) == ["1", "2"]
    assert describer._known.keys() == {"demo:1", "demo:2"}
    assert describer._asking == {}
