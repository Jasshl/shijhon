"""Focused regressions for request authentication, capture and artist fallbacks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest

from shijhon.access import AccessDenied
from shijhon.catalog.model import CatalogArtist, CatalogRef
from shijhon.collection.visibility import Visibility
from shijhon.proxy.auth import Caller
from shijhon.proxy.params import RestCall
from shijhon.views.additions import CatalogAdditions
from shijhon.views.answers import LibraryAnswer, library_answer
from shijhon.views.commits import REFUSED_DOWNLOAD, Commits
from shijhon.views.complete import CompleteAlbums
from shijhon.views.ids import CatalogId

pytestmark = pytest.mark.anyio


def call(method: str, pairs: list[tuple[str, str]]) -> RestCall:
    return RestCall.build(
        method, "POST", ("/rest/" + method).encode(), urlencode(pairs).encode(), [], None
    )


async def test_album_action_authenticates_before_any_pending_lookup() -> None:
    commits = object.__new__(Commits)
    commits.owned = SimpleNamespace(pending=AsyncMock(return_value=True), fill_album=AsyncMock())
    asked = call("star", [("albumId", str(n)) for n in range(10_000)])
    ctx = SimpleNamespace(caller=AsyncMock(return_value=None))
    await commits._album_action("star", asked, ctx)
    commits.owned.pending.assert_not_awaited()
    commits.owned.fill_album.assert_not_awaited()
    ctx.caller.return_value = Caller("listener")
    await commits._album_action("star", call("star", [("albumId", "one")]), ctx)
    commits.owned.fill_album.assert_awaited_once_with("one", "star")


@pytest.mark.parametrize("completion", [False, True])
async def test_only_completion_capture_needs_a_late_credential_proof(completion: bool) -> None:
    caller = Caller("listener")
    checker = SimpleNamespace(check=AsyncMock(return_value=caller))
    access = SimpleNamespace(verify=AsyncMock(side_effect=AccessDenied))
    upstream = SimpleNamespace(library_answer=None)
    visibility = Visibility(upstream, checker, access, None, None)
    upstream.library_answer = visibility.answer
    asked = call("search3", [("query", "Orchard")])
    if completion:
        answer = await library_answer(upstream, asked, caller=caller)
        checker.check.assert_awaited_once_with(asked, fresh=True)
    else:
        answer = await visibility.final_answer(
            asked, SimpleNamespace(caller=AsyncMock(return_value=caller))
        )
        checker.check.assert_not_awaited()
    assert answer is not None
    access.verify.assert_awaited_once_with("listener")


async def test_artist_capture_passes_the_verified_caller() -> None:
    caller = Caller("listener")
    failed = LibraryAnswer(200, [], b'{"subsonic-response":{"status":"failed"}}')
    upstream = SimpleNamespace(library_answer=AsyncMock(return_value=failed))
    additions = CatalogAdditions(SimpleNamespace(), None, upstream)
    asked = call("getArtist", [("id", "native")])
    assert await additions.artist_page(
        asked, SimpleNamespace(caller=AsyncMock(return_value=caller))
    )
    upstream.library_answer.assert_awaited_once_with(asked, caller=caller)


@pytest.mark.parametrize("detail", [False, True])
async def test_complete_capture_passes_the_verified_caller(detail: bool) -> None:
    caller = Caller("listener")
    failed = LibraryAnswer(200, [], b'{"subsonic-response":{"status":"failed"}}')
    upstream = SimpleNamespace(library_answer=AsyncMock(return_value=failed))
    fills = SimpleNamespace(
        viewable=AsyncMock(return_value=True),
        view=AsyncMock(return_value=(None, "unavailable")),
        any_shown=AsyncMock(return_value=True),
    )
    complete = CompleteAlbums(fills, upstream)
    asked = call("getAlbum" if detail else "getAlbumList2", [("id", "native")])
    ctx = SimpleNamespace(caller=AsyncMock(return_value=caller), head=False, refusal=None)
    result = (
        await complete._view(asked, ctx)
        if detail
        else await complete.wrap_list("getAlbumList2", None)(asked, ctx)
    )
    assert result is not None
    upstream.library_answer.assert_awaited_once_with(asked, caller=caller)


async def test_both_service_artist_searches_are_scoped_to_the_configured_library() -> None:
    artist = CatalogArtist(CatalogRef("demo", "artist"), "Glass Orchard")
    native = SimpleNamespace(
        library_id=7,
        subsonic=AsyncMock(
            return_value={"searchResult3": {"artist": [{"id": "native", "name": artist.name}]}}
        ),
    )
    additions = CatalogAdditions(None, None, None, navidrome=native, library_id=7)
    assert await additions._library_artist_checked(artist.name) == ("native", True)
    assert ("musicFolderId", "7") in native.subsonic.await_args.args[1]
    commits = object.__new__(Commits)
    commits.navidrome, commits._fetch = native, AsyncMock(return_value=artist)
    assert await commits._artist(CatalogId("ar", artist.ref)) == "native"
    assert ("musicFolderId", "7") in native.subsonic.await_args.args[1]


async def test_refused_native_artist_mapping_falls_back_to_the_virtual_page() -> None:
    catalog = SimpleNamespace(key="demo")
    additions = CatalogAdditions(catalog, None, None, navidrome=SimpleNamespace())
    additions._within_budget = AsyncMock(
        return_value=(CatalogArtist(CatalogRef("demo", "artist"), "Glass Orchard"), "ok")
    )
    additions._library_artist = AsyncMock(return_value="native")
    additions._navidrome = AsyncMock(
        return_value=LibraryAnswer(
            200, [], b'{"subsonic-response":{"status":"failed","error":{"code":70}}}'
        )
    )
    asked = call("getArtist", [("id", "sh.ar.demo.artist")])
    assert (
        await additions.catalog_artist_page(
            asked, SimpleNamespace(caller=AsyncMock(return_value=Caller("listener")))
        )
        is None
    )


async def test_download_refusal_names_only_the_refused_targets() -> None:
    assert "album or artist" in REFUSED_DOWNLOAD
    assert "playlist" not in REFUSED_DOWNLOAD
