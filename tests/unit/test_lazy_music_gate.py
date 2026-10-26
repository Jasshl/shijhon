"""The current library grant is required only when a handler needs a caller."""

from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from shijhon.access import AccessDenied, AccessUnavailable, Principal
from shijhon.collection.gates import MusicGate
from shijhon.proxy.app import Forward, ProxyApp, RequestContext
from shijhon.proxy.auth import Caller, CheckFailed, Refusal
from shijhon.proxy.params import RestCall
from shijhon.proxy.upstream import Upstream
from shijhon.views.complete import CompleteAlbums

pytestmark = pytest.mark.anyio
OWNER = Principal("owner-a", "Listener", False, frozenset({1}))


def call(query: bytes = b"u=listener&p=fixture-password&v=1.16.1&c=tests") -> RestCall:
    return RestCall.build("getSong", "GET", b"/rest/getSong", query, [], None)


async def test_first_caller_proves_credentials_then_grants_once() -> None:
    order = []

    async def credentials(asked, *, fresh=False):
        assert fresh
        order.append("credentials")
        return Caller("Listener")

    async def grants(username):
        assert username == "Listener"
        order.append("grants")
        return OWNER

    ctx = RequestContext(
        SimpleNamespace(check=credentials),
        call(),
        {},
        music_gate=MusicGate(SimpleNamespace(verify=grants), 1),
    )
    assert order == []
    assert await ctx.caller() == Caller("Listener")
    assert ctx.kept["shijhon.principal"] is OWNER
    assert await ctx.caller() == Caller("Listener")
    assert order == ["credentials", "grants"]


@pytest.mark.parametrize("verdict", [None, Refusal(200, [], b"native refusal")])
async def test_without_accepted_credentials_no_identity_lookup(verdict) -> None:
    checker = SimpleNamespace(check=AsyncMock(return_value=verdict))
    access = SimpleNamespace(verify=AsyncMock())
    ctx = RequestContext(checker, call(), {}, music_gate=MusicGate(access, 1))
    assert await ctx.caller() is None
    assert await ctx.caller() is None
    assert ctx.refusal is verdict
    checker.check.assert_awaited_once()
    access.verify.assert_not_awaited()


@pytest.mark.parametrize("denied", ["grant", "identity", "foreign-folder"])
async def test_missing_grant_hides_caller_and_latches_native_bypass(denied) -> None:
    checker = SimpleNamespace(check=AsyncMock(return_value=Caller("Listener")))
    access = SimpleNamespace(
        verify=AsyncMock(return_value=Principal("owner-a", "Listener", True, frozenset({2})))
    )
    if denied == "identity":
        access.verify.side_effect = AccessDenied
    asked = call(call().query + (b"&musicFolderId=2" if denied == "foreign-folder" else b""))
    if denied == "foreign-folder":  # a list: Navidrome reads the folder there
        asked = RestCall.build(
            "getAlbumList2", "GET", b"/rest/getAlbumList2", asked.query, [], None
        )
    ctx = RequestContext(checker, asked, {}, music_gate=MusicGate(access, 1))
    assert await ctx.caller() is None
    assert ctx.bypass and ctx.refusal is None
    assert await ctx.caller() is None
    checker.check.assert_awaited_once()
    assert access.verify.await_count == (0 if denied == "foreign-folder" else 1)


@pytest.mark.parametrize("folders", [b"musicFolderId=01&musicFolderId=999", b"musicFolderId=7"])
async def test_a_list_naming_a_library_the_caller_cannot_read_is_navidrome_s(
    folders: bytes,
) -> None:
    checker = SimpleNamespace(check=AsyncMock(return_value=Caller("Listener")))
    access = SimpleNamespace(
        verify=AsyncMock(return_value=Principal("owner-a", "Listener", False, frozenset({1, 7})))
    )
    query = call().query + b"&" + folders
    asked = RestCall.build("search3", "GET", b"/rest/search3", query, [], None)
    ctx = RequestContext(checker, asked, {}, music_gate=MusicGate(access, 1))
    assert await ctx.caller() is None and ctx.bypass  # (7 alone: another library's list)
    assert access.verify.await_count == (1 if b"999" in folders else 0)


@pytest.mark.parametrize(
    ("method", "folder"),
    [
        ("stream", b"999"),  # Navidrome ignores the folder: no way past Shijhon
        ("getSong", b"2"),
        ("getAlbum", b"2"),
        ("getCoverArt", b"2"),
        ("getAlbumList2", b"01"),  # this library, as Navidrome reads it
        ("search3", b"abc"),  # none: every library
        ("getAlbumList2", b"9" * 4301),
    ],
)
async def test_a_folder_navidrome_does_not_read_as_another_library_takes_no_bypass(
    method: str, folder: bytes
) -> None:
    checker = SimpleNamespace(check=AsyncMock(return_value=Caller("Listener")))
    access = SimpleNamespace(verify=AsyncMock(return_value=OWNER))
    query = call().query + b"&id=x&musicFolderId=" + folder
    asked = RestCall.build(method, "GET", b"/rest/" + method.encode(), query, [], None)
    ctx = RequestContext(checker, asked, {}, music_gate=MusicGate(access, 1))
    assert await ctx.caller() == Caller("Listener")
    assert not ctx.bypass
    access.verify.assert_awaited_once()


@pytest.mark.parametrize("failed", ["credentials", "grants"])
async def test_unavailable_proof_is_remembered_and_rethrown(failed) -> None:
    checker = SimpleNamespace(check=AsyncMock(return_value=Caller("Listener")))
    access = SimpleNamespace(verify=AsyncMock(return_value=OWNER))
    if failed == "credentials":
        checker.check.side_effect = CheckFailed("no credential verdict")
    else:
        access.verify.side_effect = AccessUnavailable
    ctx = RequestContext(checker, call(), {}, music_gate=MusicGate(access, 1))
    with pytest.raises(CheckFailed) as first:
        await ctx.caller()
    with pytest.raises(CheckFailed) as second:
        await ctx.caller()
    assert first.value is second.value
    checker.check.assert_awaited_once()
    assert access.verify.await_count == (0 if failed == "credentials" else 1)


async def proxy_request(proxy: ProxyApp) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://proxy.test"
    ) as client:
        return await client.get("/rest/getSong?u=Listener&p=fixture-password&id=sh.tr.demo.1")


async def test_noop_before_handler_and_library_answer_need_no_proof() -> None:
    checker = SimpleNamespace(check=AsyncMock(side_effect=AssertionError("no credential work")))
    access = SimpleNamespace(verify=AsyncMock(side_effect=AssertionError("no grant work")))
    upstream = Upstream("http://native.test")
    proxy = ProxyApp(upstream, checker)
    proxy.music_gate = MusicGate(access, 1)
    order = []

    async def before(call, ctx):
        order.append("before")

    async def handler(call, ctx):
        order.append("handler")

    async def library_answer(call):
        order.append("library-answer")

    async def forward(call, receive, send, **kwargs):
        order.append("forward")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"native"})

    proxy.before, proxy.handlers["getSong"] = before, handler
    upstream.library_answer = library_answer
    proxy.forward_call = forward
    try:
        assert (await proxy_request(proxy)).content == b"native"
        assert order == ["before", "handler", "library-answer", "forward"]
        checker.check.assert_not_awaited()
        access.verify.assert_not_awaited()
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("stage", ["before", "handler"])
async def test_denied_work_preserves_original_call_and_skips_final_answer(stage) -> None:
    upstream = Upstream("http://native.test")
    checker = SimpleNamespace(check=AsyncMock(return_value=Caller("Listener")))
    access = SimpleNamespace(verify=AsyncMock(side_effect=AccessDenied))
    proxy = ProxyApp(upstream, checker)
    proxy.music_gate = MusicGate(access, 1)
    upstream.library_answer = AsyncMock(side_effect=AssertionError("bypassed library answer"))
    handler_called = False

    async def before(call, ctx):
        if stage == "before":
            assert await ctx.caller() is None

    async def handler(call, ctx):
        nonlocal handler_called
        handler_called = True
        assert await ctx.caller() is None
        # Even a wrapper returning a rewritten call after denial cannot change it.
        return Forward(call.rewritten(lambda k, v: "native-id" if k == "id" else None))

    async def forward(call, receive, send, **kwargs):
        assert call.get("id") == "sh.tr.demo.1"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"native"})

    proxy.before, proxy.handlers["getSong"] = before, handler
    proxy.forward_call = forward
    try:
        assert (await proxy_request(proxy)).content == b"native"
        assert handler_called == (stage == "handler")
        upstream.library_answer.assert_not_awaited()
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("failed", ["credentials", "grants"])
async def test_optional_wrapper_cannot_swallow_unavailable_current_proof(failed) -> None:
    upstream = Upstream("http://native.test")
    checker = SimpleNamespace(check=AsyncMock(return_value=Caller("Listener")))
    access = SimpleNamespace(verify=AsyncMock(side_effect=AccessUnavailable))
    proxy = ProxyApp(upstream, checker)
    proxy.music_gate = MusicGate(access, 1)
    if failed == "credentials":
        checker.check.side_effect = CheckFailed("no credential verdict")

    async def handler(call, ctx):
        with suppress(CheckFailed):
            await ctx.caller()

    proxy.handlers["getSong"] = handler
    proxy.forward_call = AsyncMock(side_effect=AssertionError("no unverified forward"))
    try:
        response = await proxy_request(proxy)
        assert response.status_code == 502
        assert response.headers["retry-after"] == "5"
        proxy.forward_call.assert_not_awaited()
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_final_answer_native_refusal_is_sent_without_second_login(method) -> None:
    refusal = Refusal(200, [(b"content-type", b"text/plain")], b"native credential refusal")
    checker = SimpleNamespace(check=AsyncMock(return_value=refusal))
    access = SimpleNamespace(verify=AsyncMock())
    upstream = Upstream("http://native.test")
    proxy = ProxyApp(upstream, checker)
    proxy.music_gate = MusicGate(access, 1)

    async def final(call, ctx):
        assert await ctx.caller() is None

    proxy.final_library_answer = final
    upstream.library_answer = AsyncMock(side_effect=AssertionError("legacy fallback"))
    proxy.forward_call = AsyncMock(side_effect=AssertionError("second failed login"))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url="http://proxy.test"
        ) as client:
            response = await client.request(method, "/rest/search3?f=jsonp&callback=result")
        assert response.status_code == 200
        assert response.content == (refusal.body if method == "GET" else b"")
        checker.check.assert_awaited_once()
        access.verify.assert_not_awaited()
        upstream.library_answer.assert_not_awaited()
        proxy.forward_call.assert_not_awaited()
    finally:
        await upstream.aclose()


async def test_final_answer_cannot_forward_when_current_grants_unavailable() -> None:
    checker = SimpleNamespace(check=AsyncMock(return_value=Caller("Listener")))
    upstream = Upstream("http://native.test")
    proxy = ProxyApp(upstream, checker)
    proxy.music_gate = MusicGate(
        SimpleNamespace(verify=AsyncMock(side_effect=AccessUnavailable)), 1
    )

    async def final(call, ctx):
        await ctx.caller()

    proxy.final_library_answer = final
    proxy.forward_call = AsyncMock(side_effect=AssertionError("unverified forward"))
    try:
        assert (await proxy_request(proxy)).status_code == 502
        proxy.forward_call.assert_not_awaited()
    finally:
        await upstream.aclose()


@pytest.mark.parametrize("shown", [False, True])
async def test_complete_counts_require_current_grant_only_with_shown_plans(shown) -> None:
    upstream = Upstream("http://native.test")
    upstream.library_answer = AsyncMock(side_effect=AssertionError("unverified metadata"))
    fills = SimpleNamespace(any_shown=AsyncMock(return_value=shown))
    complete = CompleteAlbums(fills, upstream)
    checker = SimpleNamespace(check=AsyncMock(return_value=Caller("Listener")))
    access = SimpleNamespace(verify=AsyncMock(side_effect=AccessDenied))
    asked = RestCall.build("getAlbumList2", "GET", b"/rest/getAlbumList2", b"", [], None)
    ctx = RequestContext(checker, asked, {}, music_gate=MusicGate(access, 1))
    try:
        assert await complete.wrap_list("getAlbumList2", None)(asked, ctx) is None
        assert ctx.bypass == shown
        assert checker.check.await_count == int(shown)
        assert access.verify.await_count == int(shown)
        upstream.library_answer.assert_not_awaited()
    finally:
        await upstream.aclose()
