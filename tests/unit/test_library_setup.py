"""Setup boundaries refuse untrusted native accounts before freezing assignments."""

from __future__ import annotations

from dataclasses import replace

import pytest

from shijhon.dashboard.auth import Session
from shijhon.dashboard.library_setup import SetupAccess, SetupDenied, SetupUnavailable
from shijhon.navidrome.client import NavidromeError

pytestmark = pytest.mark.anyio


class NativeAccounts:
    library_id = 1

    def __init__(self):
        self.users = {
            "admin": {"id": "admin", "userName": "Operator", "isAdmin": True},
            "human": {"id": "human", "userName": "Listener", "isAdmin": False},
            "blocked": {"id": "blocked", "userName": "No grant", "isAdmin": False},
            "service": {"id": "service", "userName": "Machine", "isAdmin": True},
        }
        self.grants = {"admin": [1], "human": [1], "blocked": [2], "service": [1]}
        self.calls = []
        self.page = None
        self.unavailable = False

    async def native_json(self, method, path, **kwargs):
        self.calls.append((path, kwargs))
        if self.unavailable:
            raise NavidromeError("private native diagnostic")
        if path == "user":
            params = kwargs["params"]
            assert 0 < params["_end"] - params["_start"] <= 500
            values = self.page if self.page is not None else list(self.users.values())
            return values[params["_start"] : params["_end"]]
        ident = path.split("/")[1]
        if path.endswith("/library"):
            return [{"id": i} for i in self.grants[ident]]
        return self.users[ident]


def adapter(native):
    return SetupAccess(native, Session("hash", "Operator", "csrf", 1, "admin"), "Machine")


async def test_native_roster_uses_current_grants_and_excludes_service_without_credentials():
    native = NativeAccounts()
    native.users["human"]["password"] = "private-test-secret"
    access = adapter(native)
    accounts = await access.accounts()
    assert [(a.user_id, a.username, a.library_ids) for a in accounts] == [
        ("human", "Listener", frozenset({"1"})),
        ("admin", "Operator", frozenset({"1"})),
    ]
    assert "private-test-secret" not in repr(accounts)
    assert native.calls[0][0] == "user/admin"
    assert all(path != "user/service/library" for path, _ in native.calls)
    assert (await access.authorize("human")).owner_id == "human"
    with pytest.raises(SetupDenied):
        await access.authorize("service")


@pytest.mark.parametrize("change", ["role", "grant", "identity", "actor", "service"])
async def test_fresh_admin_proof_precedes_roster_and_target_metadata(change):
    native = NativeAccounts()
    access = adapter(native)
    if change == "role":
        native.users["admin"]["isAdmin"] = False
    elif change == "grant":
        native.grants["admin"] = []
    elif change == "identity":
        native.users["admin"]["id"] = "replacement"
    elif change == "actor":
        access.session = replace(access.session, user_id="")
    else:
        access.service_username = "operator"
    for action in (access.accounts, lambda: access.authorize("human")):
        with pytest.raises(SetupDenied):
            await action()
    assert not any(path in ("user", "user/human") for path, _ in native.calls)


@pytest.mark.parametrize(
    "page",
    [
        [
            {"id": "x", "userName": "Same", "isAdmin": False},
            {"id": "y", "userName": "same", "isAdmin": False},
        ],
        [
            {"id": "x", "userName": "One", "isAdmin": False},
            {"id": "x", "userName": "Two", "isAdmin": False},
        ],
        [{"id": "x", "userName": "Broken", "isAdmin": "true"}],
        [{"id": "", "userName": "Broken", "isAdmin": False}],
    ],
)
async def test_ambiguous_or_malformed_roster_refuses_the_whole_setup(page):
    native = NativeAccounts()
    native.page = page
    with pytest.raises(SetupUnavailable):
        await adapter(native).accounts()


async def test_roster_is_bounded_and_unavailable_proof_never_uses_remembered_accounts():
    native = NativeAccounts()
    native.page = [
        {"id": f"u{i}", "userName": f"Person {i}", "isAdmin": False} for i in range(10_001)
    ]
    with pytest.raises(SetupUnavailable):
        await adapter(native).accounts()
    pages = [kwargs["params"] for path, kwargs in native.calls if path == "user"]
    assert len(pages) == 21 and max(p["_end"] - p["_start"] for p in pages) == 500
    native.unavailable = True
    with pytest.raises(SetupUnavailable, match="Existing accounts could not be verified"):
        await adapter(native).accounts()
