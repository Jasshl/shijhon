"""AudioMuse-AI's token goes only to the origin (scheme, host and port) it was entered or
configured with, whatever changes the address afterwards: the dashboard, the configuration
file or the environment. A token that cannot go to the address in use is not used until it is
entered again, and the page says so."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import pytest

from shijhon.config import Settings, load_settings
from shijhon.dashboard import sections
from shijhon.dashboard.saved import (
    REMOVE,
    TOKEN_FOR,
    Saved,
    effective,
    encode,
    locked_keys,
    token_withheld,
)

A, B, C, D = (f"http://{name}.example:8000" for name in "abcd")
TA, TB = "token-of-host-a-31f0", "token-of-host-b-7c9e"  # invented

Saved_ = dict[str, dict[str, Saved]]


@pytest.fixture(autouse=True)
def _no_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's or CI's own SHIJHON_* variables must not lock settings here."""
    for name in list(os.environ):
        if name.upper().startswith("SHIJHON_"):
            monkeypatch.delenv(name)


def configure(**audiomuse: Any) -> tuple[Settings, Mapping[str, Mapping[str, str]]]:
    configured = load_settings(None, audiomuse=audiomuse)
    return configured, locked_keys(configured)


async def save(
    configured: Settings,
    locked: Mapping[str, Mapping[str, str]],
    saved: Saved_,
    form: dict[str, str],
) -> tuple[sections.Submitted, Saved_]:
    """The Sound page's save: what it refuses, and the saved values after it (the writes
    applied the way the store applies them)."""
    submitted = await sections.submit(
        "audiomuse", configured, saved, form, addon_names=[], locked=locked
    )
    if submitted.problems:
        return submitted, saved
    written = sections.writes("audiomuse", configured, saved, submitted, locked=locked)
    section = dict(saved.get("audiomuse", {}))
    for key, value in written.writes.items():
        if value is REMOVE:
            section.pop(key, None)
        else:
            section[key] = Saved(json.loads(encode(value)), 0.0, "tester")
    return submitted, {"audiomuse": section}


def in_use(
    configured: Settings, locked: Mapping[str, Mapping[str, str]], saved: Saved_
) -> tuple[str, str | None]:
    """The address and the token that apply."""
    applying = effective(configured, saved, locked=locked)[0].audiomuse
    return applying.url, applying.token.get_secret_value() if applying.token else None


def stored(url: str, token: str | None = None, entered_for: str | None = None) -> Saved_:
    """Values saved before (the token with the place it was entered for)."""
    from shijhon.dashboard.saved import place

    section = {"url": Saved(url, 0.0, "tester")}
    if token is not None:
        section["token"] = Saved(token, 0.0, "tester")
        section[TOKEN_FOR] = Saved(place(entered_for or url), 0.0, "tester")
    return {"audiomuse": section}


# --- the dashboard moves the address ---------------------------------------------------------


@pytest.mark.anyio
async def test_a_configuration_token_never_goes_to_a_third_host_by_clearing_the_dashboards() -> (
    None
):
    """The file's token is host A's; a token typed on the dashboard is host B's; clearing
    that one while moving to host C must not hand A's token to C."""
    configured, locked = configure(url=A, token=TA)
    saved: Saved_ = {}
    refused, saved = await save(configured, locked, saved, {"url": B})
    assert set(refused.problems) == {"token"}  # nothing typed: A's token would be lost
    done, saved = await save(configured, locked, saved, {"url": B, "token": TB})
    assert not done.problems and in_use(configured, locked, saved) == (B, TB)
    cleared, saved = await save(configured, locked, saved, {"url": C, "token-clear": "true"})
    assert not cleared.problems
    assert in_use(configured, locked, saved) == (C, None)  # not A's token, not B's
    assert token_withheld(configured, saved, locked=locked)  # and the page says to enter it
    # Back at A, the configuration's token belongs again.
    back, saved = await save(configured, locked, saved, {"url": A})
    assert not back.problems and in_use(configured, locked, saved) == (A, TA)
    # A token typed there and then cleared gives way to the configuration's.
    _, saved = await save(configured, locked, saved, {"token": TB})
    assert in_use(configured, locked, saved) == (A, TB)
    cleared, saved = await save(configured, locked, saved, {"token-clear": "true"})
    assert in_use(configured, locked, saved) == (A, TA) and TOKEN_FOR not in saved["audiomuse"]


@pytest.mark.anyio
async def test_a_token_typed_with_the_address_is_kept_for_that_origin_only() -> None:
    configured, locked = configure(url=A, token=TA)
    done, saved = await save(configured, locked, {}, {"url": B, "token": TB})
    assert not done.problems and saved["audiomuse"][TOKEN_FOR].value  # where it was entered
    assert in_use(configured, locked, saved) == (B, TB)
    # Another path on the same host is the same place.
    again, saved = await save(configured, locked, saved, {"url": B + "/muse"})
    assert not again.problems and in_use(configured, locked, saved) == (B + "/muse", TB)
    # Another port or scheme is another place: refused unless the token is typed again.
    for other in ("http://b.example:9000", "https://b.example:8000"):
        moved, _ = await save(configured, locked, saved, {"url": other})
        assert set(moved.problems) == {"token"}
    # The configuration's own token typed for another host is saved with it.
    same, saved = await save(configured, locked, saved, {"url": C, "token": TA})
    assert not same.problems and in_use(configured, locked, saved) == (C, TA)


@pytest.mark.anyio
async def test_a_token_removed_on_the_dashboard_stays_removed() -> None:
    configured, locked = configure(url=A, token=TA)
    removed, saved = await save(configured, locked, {}, {"token-clear": "true"})
    assert not removed.problems and in_use(configured, locked, saved) == (A, None)
    assert not token_withheld(configured, saved, locked=locked)  # (removed, not held back)


# --- the configuration or the environment moves the address ----------------------------------


def test_a_saved_token_does_not_follow_the_configurations_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = stored(B, TB)  # entered on the dashboard for host B
    configured, locked = configure(url=B)
    assert in_use(configured, locked, saved) == (B, TB)
    assert not token_withheld(configured, saved, locked=locked)
    # The file's address changes (and the dashboard's saved address is gone).
    saved["audiomuse"].pop("url")
    moved, moved_locked = configure(url=D)
    assert in_use(moved, moved_locked, saved) == (D, None)
    assert token_withheld(moved, saved, locked=moved_locked)
    # ... or the environment's address changes, over the file's and the dashboard's.
    monkeypatch.setenv("SHIJHON_AUDIOMUSE__URL", D)
    over, over_locked = configure()
    assert over_locked == {"audiomuse": {"url": "SHIJHON_AUDIOMUSE__URL"}}
    assert in_use(over, over_locked, stored(B, TB)) == (D, None)
    assert token_withheld(over, stored(B, TB), locked=over_locked)
    # Entering the token again, at the new address, takes it there.
    monkeypatch.delenv("SHIJHON_AUDIOMUSE__URL")


@pytest.mark.anyio
async def test_retyping_the_same_token_binds_it_to_the_address_in_use() -> None:
    configured, locked = configure(url=D)
    saved = stored(B, TB)
    saved["audiomuse"].pop("url")  # (the configuration's address, D, applies)
    assert in_use(configured, locked, saved) == (D, None)
    done, saved = await save(configured, locked, saved, {"url": D, "token": TB})
    assert not done.problems and in_use(configured, locked, saved) == (D, TB)
    assert not token_withheld(configured, saved, locked=locked)


# --- the environment sets the token ----------------------------------------------------------


@pytest.mark.anyio
async def test_a_token_from_the_environment_stays_with_the_configured_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHIJHON_AUDIOMUSE__TOKEN", TA)
    configured, locked = configure(url=A)
    assert locked == {"audiomuse": {"token": "SHIJHON_AUDIOMUSE__TOKEN"}}
    # The dashboard cannot move the address, and a token typed there is ignored.
    refused, saved = await save(configured, locked, {}, {"url": C, "token": TB})
    assert set(refused.problems) == {"url"}
    assert "SHIJHON_AUDIOMUSE__TOKEN" in refused.problems["url"].row
    assert TA not in refused.problems["url"].row and not saved
    # Another path on the same host is fine.
    fine, saved = await save(configured, locked, {}, {"url": A + "/muse"})
    assert not fine.problems and in_use(configured, locked, saved) == (A + "/muse", TA)
    # An address saved some other way is still not sent the environment's token.
    assert in_use(configured, locked, stored(C)) == (C, None)
    assert token_withheld(configured, stored(C), locked=locked)
    # A token saved on the dashboard is hidden by the environment's, which goes to A.
    assert in_use(configured, locked, stored(A, TB)) == (A, TA)


def test_a_token_in_the_configuration_needs_the_configurations_address() -> None:
    # The token is configured, the address is only on the dashboard.
    configured, locked = configure(token=TA)
    assert in_use(configured, locked, stored(C)) == (C, None)
    assert token_withheld(configured, stored(C), locked=locked)
    # No token anywhere: nothing is held back (AudioMuse-AI may run without one).
    open_, open_locked = configure()
    assert in_use(open_, open_locked, stored(C)) == (C, None)
    assert not token_withheld(open_, stored(C), locked=open_locked)
    # With the configuration's address, the configuration's token is used.
    fine, fine_locked = configure(url=A, token=TA)
    assert in_use(fine, fine_locked, {}) == (A, TA)
    assert in_use(fine, fine_locked, stored(A + "/muse")) == (A + "/muse", TA)
