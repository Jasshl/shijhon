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
        songs = songs[:count]
        # Simulate a native page cap independent of the requested _end.
        native.native = AsyncMock(
            return_value=httpx.Response(
                200,
                headers={"x-total-count": str(count)},
                json=[{key: song} for song in songs[:5000]],
            )
        )
        try:
            if count > 5000:
                with pytest.raises(NavidromeError, match="complete listing unavailable"):
                    await native.archive_song_ids(kind, "native-id")
            else:
                assert await native.archive_song_ids(kind, "native-id") == songs
            assert native.native.await_count == 1
            assert native.native.call_args.kwargs["params"]["_end"] == 5001
        finally:
            await native.aclose()

    anyio.run(run)


@pytest.mark.parametrize("strict", [False, True])
def test_shared_archives_bound_aggregate_entries_and_resource_lookups(strict):
    async def run():
        native = NavidromeService("http://test.invalid", "admin", "synthetic")
        interceptor = Interceptor.__new__(Interceptor)
        interceptor.navidrome = native
        native.share = AsyncMock(return_value={"resourceType": "album", "resourceIds": "a,b"})
        native.native = AsyncMock(
            side_effect=[
                httpx.Response(200, headers={"x-total-count": "3000"}, json=[{"id": "a"}] * 3000),
                httpx.Response(200, headers={"x-total-count": "2001"}, json=[{"id": "b"}] * 2001),
            ]
        )
        try:
            if strict:
                with pytest.raises(NavidromeError):
                    await interceptor._shared_songs("share", strict=True)
            else:
                assert await interceptor._shared_songs("share") == []
            assert native.native.call_args.kwargs["params"]["_end"] == 2001
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
