"""Archive inspection needs complete native totals, even at a page boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from shijhon.delivery.intercept import Interceptor
from shijhon.navidrome.client import NavidromeError, NavidromeService


@pytest.mark.parametrize("kind", ["album", "playlist"])
@pytest.mark.parametrize("count", [0, 4999, 5000, 5001])
def test_archive_listing_proves_completeness_and_preserves_repetitions(kind: str, count: int):
    async def run():
        native = NavidromeService("http://test.invalid", "admin", "synthetic")
        key = "mediaFileId" if kind == "playlist" else "id"
        songs = ["one", "two", "one"] + ["one"] * max(0, count - 3)
        songs = songs[:count] if kind == "playlist" else [f"song{i}" for i in range(count)]

        async def page(method, path, *, params):
            start, end = params["_start"], params["_end"]
            return httpx.Response(
                200,
                headers={"x-total-count": str(count)},
                json=[{"id": str(i), key: song} for i, song in enumerate(songs[start:end], start)],
            )

        native.native = AsyncMock(side_effect=page)
        try:
            assert await native.archive_song_ids(kind, "native-id") == songs
            assert native.native.await_count == (2 if count > 5000 else 1)
            assert native.native.call_args.kwargs["params"]["_end"] == (
                10000 if count > 5000 else 5000
            )
        finally:
            await native.aclose()

    anyio.run(run)


@pytest.mark.parametrize("strict", [False, True])
def test_shared_archives_page_all_entries_and_bound_resource_lookups(strict):
    async def run():
        native = NavidromeService("http://test.invalid", "admin", "synthetic")
        interceptor = Interceptor.__new__(Interceptor)
        interceptor.navidrome = native
        native.share = AsyncMock(return_value={"resourceType": "album", "resourceIds": "a,b"})
        native.native = AsyncMock(
            side_effect=[
                httpx.Response(
                    200,
                    headers={"x-total-count": "3000"},
                    json=[{"id": f"a{i}"} for i in range(3000)],
                ),
                httpx.Response(
                    200,
                    headers={"x-total-count": "2001"},
                    json=[{"id": f"b{i}"} for i in range(2001)],
                ),
            ]
        )
        try:
            assert len(await interceptor._shared_songs("share", strict=strict)) == 5001
            native.share.return_value["resourceIds"] = ",".join(["album"] * 21)
            native.native.reset_mock()
            if strict:
                with pytest.raises(NavidromeError):
                    await interceptor._shared_songs("share", strict=True)
            else:
                assert await interceptor._shared_songs("share") == []
            native.native.assert_not_awaited()
        finally:
            await native.aclose()

    anyio.run(run)


@pytest.mark.parametrize("share", [None, {}, {"resourceIds": "album", "resourceType": "unknown"}])
def test_unknown_shared_resources_cannot_pass_the_final_listing_gate(share):
    async def run():
        interceptor = Interceptor.__new__(Interceptor)
        interceptor.navidrome = SimpleNamespace(share=AsyncMock(return_value=share))
        assert await interceptor._shared_songs("share") == []
        with pytest.raises(NavidromeError):
            await interceptor._shared_songs("share", strict=True)

    anyio.run(run)


@pytest.mark.parametrize(
    ("total", "entries"),
    [
        (None, []),
        ("unknown", []),
        ("-1", []),
        ("999999999999999999999", []),
        ("2", [{"mediaFileId": "one"}]),  # truncated below the inspection limit
        ("0", [{"mediaFileId": "one"}]),
        ("1", {}),
        ("1", [None]),
        ("1", [{"id": "playlist-entry-id"}]),
        ("1", [{"mediaFileId": 123}]),
        ("1", [{"mediaFileId": ""}]),
    ],
)
def test_archive_listing_rejects_missing_totals_partial_and_malformed_rows(total, entries):
    async def run():
        native = NavidromeService("http://test.invalid", "admin", "synthetic")
        native.native = AsyncMock(
            return_value=httpx.Response(
                200, headers={"x-total-count": total} if total is not None else {}, json=entries
            )
        )
        try:
            with pytest.raises(NavidromeError):
                await native.archive_song_ids("playlist", "native-id")
        finally:
            await native.aclose()

    anyio.run(run)


@pytest.mark.parametrize("count", [0, 5000, 5001, 10001])
@pytest.mark.parametrize("silent_tail", [False, True])
def test_playlist_download_checks_the_whole_listing(count, silent_tail):
    async def run():
        native = NavidromeService("http://test.invalid", "admin", "synthetic")

        async def page(method, path, *, params):
            return httpx.Response(
                200,
                headers={"x-total-count": str(count)},
                json=[
                    {
                        "id": str(i),
                        "mediaFileId": "silent" if silent_tail and i == count - 1 else "owned",
                    }
                    for i in range(params["_start"], min(count, params["_end"]))
                ],
            )

        native.native = AsyncMock(side_effect=page)
        interceptor = Interceptor.__new__(Interceptor)
        interceptor.navidrome = native
        interceptor.engine = SimpleNamespace(left=lambda: False, interrupted=lambda: {})
        interceptor.store = SimpleNamespace(fetchone=AsyncMock(return_value=None))
        interceptor._artist_albums = AsyncMock(return_value=[])
        interceptor.silent_among = AsyncMock(side_effect=lambda songs: "silent" in songs)
        try:
            assert await interceptor._collection_with_silence("playlist") is (
                silent_tail and count > 0
            )
            songs = interceptor.silent_among.call_args.args[0]
            assert len(songs) == count
            assert native.native.await_count == max(1, (count + 4999) // 5000)
        finally:
            await native.aclose()

    anyio.run(run)


@pytest.mark.parametrize("status", [404, 403, 502, None])
@pytest.mark.parametrize("unfinished", [False, True])
def test_playlist_lookup_errors_keep_the_previous_unfinished_write_guard(status, unfinished):
    async def run():
        interceptor = Interceptor.__new__(Interceptor)
        interceptor.navidrome = SimpleNamespace(
            playlist_song_ids=AsyncMock(side_effect=NavidromeError("unavailable", status=status)),
            songs_of_album=AsyncMock(return_value=[]),
        )
        interceptor.engine = SimpleNamespace(left=lambda: unfinished, interrupted=lambda: {})
        interceptor.store = SimpleNamespace(fetchone=AsyncMock(return_value=None))
        interceptor._artist_albums = AsyncMock(return_value=[])
        interceptor._unfinished_among = AsyncMock(return_value=False)
        assert await interceptor._collection_with_silence("playlist") is (
            unfinished and status != 404
        )

    anyio.run(run)


@pytest.mark.parametrize("failure", ["total-change", "repeated-page", "empty-page"])
def test_archive_listing_refuses_inconsistent_pages(failure):
    async def run():
        native = NavidromeService("http://test.invalid", "admin", "synthetic")
        rows = [{"id": str(i), "mediaFileId": "owned"} for i in range(5000)]
        second = (
            []
            if failure == "empty-page"
            else rows[:1]
            if failure == "repeated-page"
            else [{"id": "5000", "mediaFileId": "owned"}]
        )
        native.native = AsyncMock(
            side_effect=[
                httpx.Response(200, headers={"x-total-count": "5001"}, json=rows),
                httpx.Response(
                    200,
                    headers={"x-total-count": "5002" if failure == "total-change" else "5001"},
                    json=second,
                ),
            ]
        )
        try:
            with pytest.raises(NavidromeError):
                await native.playlist_song_ids("playlist")
        finally:
            await native.aclose()

    anyio.run(run)
