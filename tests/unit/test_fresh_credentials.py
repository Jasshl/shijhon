"""Current music authority cannot reuse an earlier accepted credential proof."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from shijhon.proxy.app import RequestContext
from shijhon.proxy.auth import Caller, CheckFailed, Refusal
from tests.unit.test_credential_check import OK, WRONG, Navidrome, answer, call, checker

pytestmark = pytest.mark.anyio


async def test_fresh_checks_bypass_positive_cache_and_refusal_evicts_it() -> None:
    credentials = checker(Navidrome(answer(**OK), answer("failed", **WRONG)))
    asked = call()
    assert await credentials.check(asked) == Caller("listener")
    assert await credentials.check(asked) == Caller("listener")
    assert credentials.pings == 1
    assert isinstance(await credentials.check(asked, fresh=True), Refusal)
    assert credentials.known(asked) is None
    assert isinstance(await credentials.check(asked), Refusal)
    assert credentials.pings == 3  # no cached success or negative cache after refusal


async def test_successful_fresh_proof_keeps_standalone_cache_compatibility() -> None:
    credentials = checker(Navidrome(answer(**OK)))
    assert await credentials.check(call(), fresh=True) == Caller("listener")
    assert await credentials.check(call()) == Caller("listener")
    assert credentials.pings == 1
    assert await credentials.check(call(), fresh=True) == Caller("listener")
    assert credentials.pings == 2


@pytest.mark.parametrize("verdict", ["accepted", "refused", "unavailable"])
async def test_identical_fresh_checks_coalesce_inflight(verdict) -> None:
    native = Navidrome(answer(**OK))
    credentials = checker(native)
    assert await credentials.check(call()) == Caller("listener")
    # Hold the next native check with a warm positive cache already present.
    original = native.send
    started, release = anyio.Event(), anyio.Event()
    native.answers.append(
        answer(**OK)
        if verdict == "accepted"
        else answer("failed", **WRONG)
        if verdict == "refused"
        else httpx.ConnectError("unavailable")
    )

    async def send(*args):
        started.set()
        await release.wait()
        return await original(*args)

    native.send = send
    outcomes = []

    async def one():
        try:
            outcomes.append(await credentials.check(call(), fresh=True))
        except CheckFailed as exc:
            outcomes.append(exc)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(one)
        await started.wait()
        for _ in range(3):
            tasks.start_soon(one)
        await anyio.wait_all_tasks_blocked()
        release.set()
    expected = {"accepted": Caller, "refused": Refusal, "unavailable": CheckFailed}[verdict]
    assert len(outcomes) == 4 and all(isinstance(outcome, expected) for outcome in outcomes)
    assert credentials.pings == 2
    assert len(native.paths) == 2


@pytest.mark.parametrize("gated", [False, True])
async def test_music_request_requires_fresh_proof_while_standalone_keeps_cache(gated) -> None:
    credentials = checker(Navidrome(answer(**OK), answer("failed", **WRONG)))
    gate = AsyncMock(return_value=True) if gated else None
    assert await credentials.check(call()) == Caller("listener")
    first = RequestContext(credentials, call(), {}, music_gate=gate)
    verdict = None if gated else Caller("listener")
    assert await first.caller() == verdict
    assert await first.caller() == verdict
    assert credentials.pings == (2 if gated else 1)
    assert isinstance(first.refusal, Refusal) == gated
    if gate is not None:
        gate.assert_not_awaited()  # rejected before any account/grant lookup


async def test_accepted_music_request_reuses_fresh_proof_inside_gate_and_handlers() -> None:
    credentials = checker(Navidrome(answer(**OK)))

    async def gate(call, ctx):
        assert await ctx.caller() == Caller("listener")
        return True

    for _ in range(2):
        ctx = RequestContext(credentials, call(), {}, music_gate=gate)
        assert await ctx.caller() == Caller("listener")
        assert await ctx.caller() == Caller("listener")
    assert credentials.pings == 2


async def test_fresh_check_without_credentials_does_not_ask_native() -> None:
    credentials = checker(SimpleNamespace(send=AsyncMock()))
    asked = call()
    asked.params.clear()
    assert await credentials.check(asked, fresh=True) is None
    assert credentials.pings == 0
