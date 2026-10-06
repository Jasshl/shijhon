"""Shared fixtures. Suites that need a special Navidrome (sharing, jukebox, watcher) use
``navidrome_factory`` with extra environment.

Suite P, the upgrade canary: ``-m canary`` selects the suites a Navidrome
upgrade must pass first - D (tag matrix), E (ID survival), C (interception and commits),
the entry contract (``test_J_contract.py``) and the harness's smoke test (which Navidrome
answers). With ``SHIJHON_NAVIDROME_VERSION`` naming a candidate release (or ``latest``),
they run against it instead of the pinned version."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from shijhon.app import create_app
from shijhon.catalog import plugin
from shijhon.config import load_settings
from tests.harness.navidrome import (
    ADMIN_PASSWORD,
    ADMIN_USER,
    PINNED_VERSION,
    NavidromeInstance,
    resolve_version,
    wanted_version,
)
from tests.harness.running import RunningServer
from tests.harness.sample_adapter import KIND, SAMPLE

# A made-up catalog adapter with settings of its own: ``[catalog] kind = "sample"``.
plugin.register(KIND, SAMPLE)
NavidromeFactory = Callable[..., NavidromeInstance]
CANARY_MODULES = (
    "test_B_credentials.py",  # the credential check reads Navidrome's answers
    "test_B_identity.py",  # ... and passes its refusals on as its own
    "test_B_limiter.py",  # Navidrome's login limit behind Shijhon
    "test_C_",
    "test_D_",
    "test_E_",
    "test_J_contract.py",
    "test_harness_smoke.py",
)


def pytest_configure(config: pytest.Config) -> None:
    # "latest" is resolved once, before pytest-xdist starts its workers: they inherit the
    # environment, so every worker runs the same release.
    if os.environ.get("SHIJHON_NAVIDROME_VERSION") == "latest":
        try:
            os.environ["SHIJHON_NAVIDROME_VERSION"] = resolve_version("latest")
        except (httpx.HTTPError, RuntimeError) as exc:
            raise pytest.UsageError(f"SHIJHON_NAVIDROME_VERSION=latest: {exc}") from None


def pytest_report_header(config: pytest.Config) -> str:
    version = wanted_version()
    kind = "pinned" if version == PINNED_VERSION else "candidate"
    local = ", local binary" if os.environ.get("SHIJHON_NAVIDROME_BIN") else ""
    return f"navidrome: {version} ({kind}{local})"


@pytest.hookimpl(tryfirst=True)  # before ``-m`` deselects by marker
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.parent.name == "acceptance" and item.path.name.startswith(CANARY_MODULES):
            item.add_marker(pytest.mark.canary)


@pytest.fixture(scope="module")
def navidrome_factory(tmp_path_factory: pytest.TempPathFactory) -> Iterator[NavidromeFactory]:
    started: list[NavidromeInstance] = []

    def make(
        env: dict[str, str | None] | None = None, *, root: Path | None = None
    ) -> NavidromeInstance:
        instance = NavidromeInstance(root or tmp_path_factory.mktemp("navidrome"), env=env)
        started.append(instance)  # (stopped also when its start fails half way)
        instance.start()
        instance.create_admin()
        return instance

    yield make
    for instance in started:
        instance.stop()


@pytest.fixture(scope="module")
def navidrome(navidrome_factory: NavidromeFactory) -> NavidromeInstance:
    """A fresh Navidrome per test module with an empty library."""
    return navidrome_factory()


@pytest.fixture(scope="module")
def shijhon_factory(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Callable[..., RunningServer]]:
    """Start Shijhon in front of a Navidrome instance; stopped at module end."""
    servers: list[RunningServer] = []

    def make(navidrome: NavidromeInstance, **overrides: object) -> RunningServer:
        settings = load_settings(
            None,
            state_dir=tmp_path_factory.mktemp("shijhon-state"),
            navidrome={
                "url": navidrome.base_url,
                "user": ADMIN_USER,
                "password": ADMIN_PASSWORD,
                "library_path": navidrome.music,
            },
            **overrides,
        )
        server = RunningServer(create_app(settings))
        server.start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
