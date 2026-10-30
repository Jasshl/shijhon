from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import shijhon.collection.reader as reader_module
from shijhon.access import AccessDenied, AccessUnavailable, Principal
from shijhon.collection.contracts import CollectionError
from shijhon.collection.gates import MusicGate
from shijhon.collection.reader import CallerLibrary, SourceRefresh
from shijhon.playlists.model import Connection
from shijhon.playlists.prompt import context
from shijhon.proxy.auth import CheckFailed
from shijhon.proxy.params import RestCall

pytestmark = pytest.mark.anyio
OWNER = Principal("owner-a", "Listener", False, frozenset({1}))


def reader():
    playlists = SimpleNamespace(
        client=SimpleNamespace(native=SimpleNamespace(library_id=1, base_url="http://native")),
        connection=AsyncMock(side_effect=AssertionError("No stored credentials needed")),
    )
    result = CallerLibrary(playlists)
    result.access.verify_native = AsyncMock(return_value=OWNER)
    result.remember(OWNER, "original-caller-bearer")
    return result


async def test_active_native_proofs_refresh_idle_expiry_but_unavailable_proofs_do_not(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(reader_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    source = reader()
    other = Principal("owner-b", "Other listener", False, frozenset({1}))
    source.remember(other, "other-bearer")
    try:
        for moment in (3500.0, 7000.0):
            now[0] = moment
            assert (await source.authorize("owner-a")).owner_id == "owner-a"
        assert source.access.verify_native.await_count == 2
        assert not source.has_bearer("owner-b")  # activity never refreshes another owner
        now[0] = 10500.0
        source.access.verify_native.side_effect = AccessUnavailable
        with pytest.raises(CollectionError):
            await source.authorize("owner-a")
        now[0] = 10601.0
        assert not source.has_bearer("owner-a")
    finally:
        await source.aclose()


@pytest.mark.parametrize("replacement", [None, "replacement-bearer"])
async def test_in_flight_native_proof_does_not_restore_evicted_or_replaced_bearer(replacement):
    source = reader()

    async def verified(token):
        source._bearers.clear()
        if replacement:
            source.remember(OWNER, replacement)
        return OWNER

    source.access.verify_native.side_effect = verified
    try:
        await source._native("owner-a")
        assert source._token("owner-a") == (replacement or "")
    finally:
        await source.aclose()


async def test_denied_older_native_proof_does_not_forget_concurrent_sign_in():
    source = reader()

    async def denied(token):
        source.remember(OWNER, "replacement-bearer")
        raise AccessDenied

    source.access.verify_native.side_effect = denied
    try:
        with pytest.raises(CollectionError):
            await source._native("owner-a")
        assert source._token("owner-a") == "replacement-bearer"
        source.access.verify_native.side_effect = None
        assert (await source.authorize("owner-a")).owner_id == "owner-a"
    finally:
        await source.aclose()


async def test_native_pages_use_only_original_caller_and_stable_bounded_library_query():
    source = reader()
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(
            200,
            headers={"x-total-count": "501"},
            json=[{"id": "one", "libraryId": 1, "path": "Original/a.flac"}],
        )

    await source.http.aclose()
    source.http = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        cookies={"ambient": "admin-cookie"},
        headers={"authorization": "Bearer ambient-admin"},
        auth=("ambient-admin", "invented-password"),
    )
    try:
        assert (await source.songs("owner-a", "1", 500, 500))[0]["id"] == "one"
        assert seen[0].url.params == httpx.QueryParams(
            library_id="1", _start="500", _end="1000", _sort="id", _order="ASC", missing="false"
        )
        assert seen[0].headers["x-nd-authorization"] == "Bearer original-caller-bearer"
        assert "authorization" not in seen[0].headers and "cookie" not in seen[0].headers
        with pytest.raises(CollectionError):
            await source.songs("owner-a", "2", 0, 500)
        with pytest.raises(CollectionError):
            await source.songs("owner-a", "1", 0, 501)
        assert len(seen) == 1
    finally:
        await source.aclose()


async def test_revocation_identity_mismatch_and_unexpected_library_never_publish_metadata():
    source = reader()
    try:
        source.access.verify_native.side_effect = AccessDenied
        with pytest.raises(CollectionError):
            await source._native("owner-a")
        assert source._token("owner-a") == ""
        source.access.verify_native.side_effect = None
        source.access.verify_native.return_value = Principal(
            "recreated", "Listener", False, frozenset({1})
        )
        source.remember(OWNER, "original-caller-bearer")
        with pytest.raises(CollectionError):
            await source._native("owner-a")
        source.access.verify_native.return_value = OWNER
        source.remember(OWNER, "original-caller-bearer")
        source._read = AsyncMock(
            return_value=(b'[{"id":"private","libraryId":2}]', "application/json")
        )
        with pytest.raises(CollectionError):
            await source.songs("owner-a", "1", 0, 500)
    finally:
        await source.aclose()


@pytest.mark.parametrize(
    "status,total,offset", [(200, "", 0), (200, "2", 0), (200, "1001", 500), (401, "1", 0)]
)
async def test_partial_changed_or_unauthenticated_native_pages_are_refused(status, total, offset):
    source = reader()
    await source.http.aclose()
    source._totals["owner-a"] = 1000
    source.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, headers={"x-total-count": total}, json=[{"id": "one", "libraryId": 1}]
            )
        )
    )
    try:
        with pytest.raises(CollectionError):
            await source.songs("owner-a", "1", offset, 500)
    finally:
        await source.aclose()


async def test_native_response_caps_and_redirect_refusal():
    source = reader()
    await source.http.aclose()
    replies = [
        httpx.Response(200, content=b"12345"),
        httpx.Response(302, headers={"location": "https://elsewhere.invalid/"}),
    ]
    source.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: replies.pop(0)))
    try:
        for _ in range(2):
            with pytest.raises(CollectionError):
                await source._read("/api/song", params={}, maximum=4)
    finally:
        await source.aclose()


async def test_expired_native_bearer_can_use_independently_valid_saved_authorization():
    source = reader()
    connection = Connection(user_id="owner-a", username="Listener", token="invented", salt="salt")
    source.playlists.connection = AsyncMock(return_value=connection)
    source.playlists.client.principal = AsyncMock(return_value=OWNER)
    try:
        source.access.verify_native.side_effect = AccessUnavailable
        with pytest.raises(CollectionError):
            await source.authorize("owner-a")
        source.playlists.connection.assert_not_awaited()
        source.access.verify_native.side_effect = AccessDenied
        assert (await source.authorize("owner-a")).owner_id == "owner-a"
        source.playlists.connection.assert_awaited_once_with("owner-a")
    finally:
        await source.aclose()


async def test_source_refresh_coalesces_and_retries_after_spawn_refusal():
    source = reader()
    queued = []
    refresh = AsyncMock()
    callback = SourceRefresh(source, refresh, lambda work: queued.append(work) or True)
    try:
        await callback.verified(OWNER, "caller")
        await callback.verified(OWNER, "caller")
        assert len(queued) == 1
        await queued.pop()()
        refresh.assert_awaited_once_with("owner-a", "1")
        await callback.verified(OWNER, "caller")
        assert not queued
        rejected = SourceRefresh(source, refresh, lambda work: False)
        await rejected.verified(OWNER, "caller")
        assert not rejected._running
    finally:
        await source.aclose()


async def test_import_lookup_uses_exact_caller_evidence_and_rechecks_grants():
    source = reader()
    path = "_imports/0123456789abcdef.flac"
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(
            200,
            headers={"x-total-count": "2"},
            json=[
                {"id": "prefix-neighbour", "libraryId": 1, "path": path + ".other", "size": 23},
                {"id": "imported", "libraryId": 1, "path": path, "size": 123},
            ],
        )

    await source.http.aclose()
    source.http = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        cookies={"ambient": "admin"},
        headers={"authorization": "Bearer admin"},
        auth=("admin", "invented"),
    )
    try:
        evidence = await source.imported_file("owner-a", "1", path)
        assert evidence and (evidence.song_id, evidence.path, evidence.size) == (
            "imported",
            path,
            123,
        )
        assert seen[0].url.params["path"] == path
        assert seen[0].url.params["_end"] == "2"
        assert seen[0].headers["x-nd-authorization"] == "Bearer original-caller-bearer"
        assert "authorization" not in seen[0].headers and "cookie" not in seen[0].headers
        assert source.access.verify_native.await_count == 2
        source.access.verify_native.side_effect = [
            OWNER,
            Principal("owner-a", "Listener", False, frozenset({2})),
        ]
        with pytest.raises(CollectionError):
            await source.imported_file("owner-a", "1", path)
    finally:
        await source.aclose()


@pytest.mark.parametrize(
    "rows,total",
    [
        ([{"id": "x", "libraryId": 2, "path": "_imports/x.flac", "size": 12}], "1"),
        ([{"id": "x", "libraryId": 1, "path": "_imports/x.flac", "size": True}], "1"),
        (
            [{"id": "x", "libraryId": 1, "path": "_imports/x.flac", "size": 12, "missing": True}],
            "1",
        ),
        ([{"id": "x", "libraryId": 1, "path": "_imports/x.flac", "size": 12}] * 2, "2"),
        ([], "3"),
    ],
)
async def test_import_lookup_refuses_ambiguous_or_foreign_native_evidence(rows, total):
    source = reader()
    await source.http.aclose()
    source.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"x-total-count": total}, json=rows)
        )
    )
    try:
        with pytest.raises(CollectionError):
            await source.imported_file("owner-a", "1", "_imports/x.flac")
    finally:
        await source.aclose()


@pytest.mark.parametrize("missing", [False, True])
async def test_import_absence_includes_missing_rows_and_exact_prefix_neighbours(missing):
    source = reader()
    path = "_imports/Song.flac"
    seen = []
    rows = [{"id": "old", "libraryId": 1, "path": path, "missing": missing}]

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, headers={"x-total-count": str(len(rows))}, json=rows)

    await source.http.aclose()
    source.http = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        headers={"authorization": "Bearer ambient"},
        cookies={"ambient": "admin"},
        auth=("admin", "invented"),
    )
    try:
        assert not await source.imported_file_absent("owner-a", "1", path)
        rows[0]["path"] = path + ".other"
        assert await source.imported_file_absent("owner-a", "1", path)
        rows.clear()
        assert await source.imported_file_absent("owner-a", "1", path)
        assert source.access.verify_native.await_count == 6
        assert all("missing" not in r.url.params and r.url.params["_end"] == "2" for r in seen)
        assert all(r.headers["x-nd-authorization"] == "Bearer original-caller-bearer" for r in seen)
        assert all("authorization" not in r.headers and "cookie" not in r.headers for r in seen)
    finally:
        await source.aclose()


@pytest.mark.parametrize(
    "failure",
    [
        "wrong-path",
        "wrong-library",
        "truncated",
        "overflow",
        "expired",
        "changed-owner",
        "changed-library",
    ],
)
async def test_import_absence_never_uses_unbound_or_incomplete_negative_evidence(failure):
    source = reader()
    path = "_imports/x.flac"
    rows = []
    total = "0"
    if failure in ("wrong-path", "wrong-library"):
        rows = [
            {
                "id": "x",
                "libraryId": 2 if failure == "wrong-library" else 1,
                "path": "unrelated.flac" if failure == "wrong-path" else path,
            }
        ]
        total = "1"
    elif failure in ("truncated", "overflow"):
        total = "1" if failure == "truncated" else "3"
    elif failure == "expired":
        source._bearers.clear()
    elif failure in ("changed-owner", "changed-library"):
        source.access.verify_native.side_effect = [
            OWNER,
            Principal(
                "other" if failure == "changed-owner" else "owner-a",
                "Listener",
                False,
                frozenset({2}) if failure == "changed-library" else frozenset({1}),
            ),
        ]

    def upstream(request):
        return httpx.Response(200, headers={"x-total-count": total}, json=rows)

    await source.http.aclose()
    source.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(CollectionError):
            await source.imported_file_absent("owner-a", "1", path)
    finally:
        await source.aclose()


async def test_music_gate_proves_credentials_before_current_grants_and_fails_closed():
    access = SimpleNamespace(verify=AsyncMock(return_value=OWNER))
    gate = MusicGate(access, 1)
    call = RestCall.build("getAlbumList2", "GET", b"/rest/getAlbumList2", b"", [], None)
    other = RestCall.build("getAlbumList2", "GET", b"/rest/x", b"musicFolderId=2", [], None)
    ctx = SimpleNamespace(caller=AsyncMock(return_value=None), kept={})
    assert not await gate(call, ctx)
    access.verify.assert_not_awaited()
    ctx.caller.return_value = SimpleNamespace(username="Listener")
    assert await gate(call, ctx)
    assert not await gate(other, ctx)  # another library's list: Navidrome's own
    access.verify.return_value = Principal("owner-a", "Listener", False, frozenset({2}))
    assert not await gate(call, ctx)
    access.verify.side_effect = AccessUnavailable
    with pytest.raises(CheckFailed):
        await gate(call, ctx)


async def test_prompt_library_context_uses_selected_names_without_broad_native_read():
    provider = AsyncMock(
        return_value=[{"artist": "Glass Orchard", "title": "First Light", "album": "Paper Garden"}]
    )
    service = SimpleNamespace(
        library_context=provider,
        client=SimpleNamespace(
            call=AsyncMock(side_effect=AssertionError("No unfiltered library read"))
        ),
    )
    connection = Connection(user_id="owner-a", username="Listener", token="invented", salt="salt")
    result = await context(service, connection, history=False, library=True)
    assert result == {"library": provider.return_value}
    provider.assert_awaited_once_with("owner-a", 100)


@pytest.mark.parametrize("failure", ["total", "duplicate", "empty", "short", "invalid"])
async def test_inconsistent_native_favorites_keep_previous_set_and_retry(tmp_path, failure):
    import anyio

    from shijhon.collection.reasons import Reasons
    from shijhon.store import Store

    source = reader()
    broken = True

    def upstream(request):
        offset = int(request.url.params["_start"])
        if request.url.path == "/api/album":
            return httpx.Response(200, headers={"x-total-count": "0"}, json=[])
        rows = [{"id": f"s{i}", "starred": True} for i in range(offset, min(offset + 500, 501))]
        total = 501
        if broken:
            if offset and failure == "total":
                total = 502
            if offset and failure == "duplicate":
                rows[0]["id"] = "s0"
            if offset and failure == "empty":
                rows = []
            if not offset and failure == "short":
                rows.pop()
            if offset and failure == "invalid":
                rows[0]["starred"] = False
        return httpx.Response(200, headers={"x-total-count": str(total)}, json=rows)

    await source.http.aclose()
    source.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    store = await Store.open(tmp_path / "favorites.db")
    try:
        await store.execute("INSERT INTO collection_favorites VALUES ('owner-a','1','song','old')")
        async with anyio.create_task_group() as group:
            reasons = Reasons(store, "1", None, lambda work: group.start_soon(work) or True)
            await reasons.due_with("owner-a", lambda: source.starred("owner-a"))
            rows = await store.fetchall("SELECT item_id FROM collection_favorites")
            assert [row["item_id"] for row in rows] == ["old"]
            assert reasons._due["owner-a"] > reader_module.time.monotonic()
            assert not await store.fetchone("SELECT 1 FROM collection_favorites_read")
            broken = False
            reasons._due["owner-a"] = 0
            await reasons.due_with("owner-a", lambda: source.starred("owner-a"))
            rows = await store.fetchall("SELECT item_id FROM collection_favorites")
            assert {row["item_id"] for row in rows} == {f"s{i}" for i in range(501)}
    finally:
        await source.aclose()
        await store.close()


@pytest.mark.parametrize("operation", ["song", "starred", "imported_file", "imported_file_absent"])
async def test_native_token_refresh_during_reads_keeps_the_same_principal(operation):
    source = reader()
    calls = 0

    def upstream(request):
        nonlocal calls
        calls += 1
        source.remember(OWNER, f"refreshed-{calls}")
        row = {
            "id": "one",
            "libraryId": 1,
            "path": "_imports/one.flac",
            "size": 10,
            "starred": True,
        }
        body = row if operation == "song" else [row] if operation == "starred" else []
        return httpx.Response(200, headers={"x-total-count": str(len(body))}, json=body)

    await source.http.aclose()
    source.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        if operation == "song":
            assert (await source.song("owner-a", "1", "one"))["id"] == "one"
        elif operation == "starred":
            assert await source.starred("owner-a") == {("song", "one"), ("album", "one")}
        elif operation == "imported_file":
            assert await source.imported_file("owner-a", "1", "_imports/one.flac") is None
        else:
            assert await source.imported_file_absent("owner-a", "1", "_imports/one.flac")
        assert source.access.verify_native.call_args.args == (f"refreshed-{calls}",)
    finally:
        await source.aclose()
