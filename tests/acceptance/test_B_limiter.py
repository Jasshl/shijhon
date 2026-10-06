"""Suite B — Navidrome's own login limit behind Shijhon.

Navidrome 0.64.2 refuses a user's logins from a client address after 5 failed ones within
20 seconds, the right password included, and allows 5 logins in 20 seconds per address at
``/auth/login``. The other suites run with that limit out of the way; this one runs with
Navidrome's default, and with Shijhon's address among Navidrome's trusted sources, as
deployed. The tests' own connection is Shijhon's trusted proxy (loopback): each device is
the address it names in ``X-Forwarded-For``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

LIMIT = 5  # Navidrome's AuthRequestLimit ...
WINDOW_SECONDS = 20.0  # ... per AuthWindowLength
SERVICE = ADMIN_USER  # Shijhon's service account in the harness


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory(
        {"ND_AUTHREQUESTLIMIT": None, "ND_EXTAUTH_TRUSTEDSOURCES": "127.0.0.1/32"}
    )
    nd.create_user("listener", "listener-password")
    nd.create_user("other", "other-password")
    with delivery_world(nd, tmp_path_factory.mktemp("b-limiter")) as w:
        yield w


class Device:
    """A client at ``address``, behind the reverse proxy in front of Shijhon."""

    def __init__(self, base_url: str, address: str) -> None:
        self.http = httpx.Client(
            base_url=base_url, headers={"X-Forwarded-For": address}, timeout=30
        )

    def ask(self, method: str, user: str, password: str, **params: str) -> httpx.Response:
        query = {"u": user, "p": password, "v": "1.16.1", "c": "limiter", "f": "json", **params}
        return self.http.get(f"/rest/{method}", params=query)

    def refused(self, method: str, user: str, password: str, **params: str) -> bool:
        body: dict[str, Any] = self.ask(method, user, password, **params).json()
        answer = body["subsonic-response"]
        return bool(answer["status"] == "failed" and answer["error"]["code"] == 40)

    def works(self, user: str, password: str) -> bool:
        """A request Shijhon forwards (ping), and one whose credentials it checks itself
        (jukeboxControl: Navidrome's "not implemented" for a caller it accepts)."""
        ping = self.ask("ping", user, password).json()["subsonic-response"]["status"] == "ok"
        return (
            ping and self.ask("jukeboxControl", user, password, action="status").status_code == 501
        )


Devices = Callable[[int], Device]


@pytest.fixture
def devices(world: DeliveryWorld) -> Iterator[Devices]:
    """Devices by number: each at an address of its own (Navidrome counts per address)."""
    made: list[Device] = []

    def device(number: int) -> Device:
        made.append(Device(world.server.base_url, f"203.0.113.{number}"))
        return made[-1]

    yield device
    for one in made:
        one.http.close()


def in_one_window(scenario: Callable[[int], None], tries: int = 3) -> None:
    """Run ``scenario(n)`` within one of Navidrome's windows: its counts start over 20
    seconds after a client's first login, so on a machine too slow for that the outcome -
    passed or failed - says nothing, and the scenario runs again with addresses of its
    own (``n``: 0, 10, 20; added to its device numbers)."""
    for attempt in range(tries):
        started, failure = time.monotonic(), None
        try:
            scenario(10 * attempt)
        except AssertionError as exc:
            failure = exc
        if time.monotonic() - started < WINDOW_SECONDS / 2 or attempt == tries - 1:
            if failure is not None:
                raise failure
            return


def test_a_stale_password_on_one_device_does_not_lock_the_users_other_device(
    devices: Devices,
) -> None:
    def scenario(n: int) -> None:
        stale, phone = devices(100 + n), devices(101 + n)
        assert phone.works("listener", "listener-password")
        for turn in range(2 * LIMIT):  # requests Shijhon checks, and requests it forwards
            method = "jukeboxControl" if turn % 2 else "ping"
            assert stale.refused(method, "listener", "old-password", action="status")
            assert phone.works("listener", "listener-password"), turn  # all the while
        # Navidrome's limit is on, and counts that device: even the right password is
        # refused there now, on a forwarded request and on a checked one.
        assert stale.refused("ping", "listener", "listener-password")
        assert stale.refused("jukeboxControl", "listener", "listener-password", action="status")
        assert phone.works("listener", "listener-password")
        # ... and another user at the stale device's address is not locked either.
        assert stale.works("other", "other-password")

    in_one_window(scenario)


def test_a_checked_request_is_one_failed_login_not_two(devices: Devices) -> None:
    """Four wrong passwords on requests Shijhon checks stay under Navidrome's limit of
    five; counted twice each (the check, then the forward) they would lock the device."""

    def scenario(n: int) -> None:
        device = devices(140 + n)
        for _ in range(LIMIT - 1):
            assert device.refused("jukeboxControl", "other", "wrong", action="status")
        assert device.works("other", "other-password")

    in_one_window(scenario)


def test_bad_logins_from_anyone_do_not_lock_the_service_account(
    world: DeliveryWorld, devices: Devices
) -> None:
    """Anyone who reaches Shijhon may guess at the service account's password: Navidrome
    locks that client's address, not Shijhon's own requests - scans, verification, the
    native API."""

    def scenario(n: int) -> None:
        attacker, elsewhere = devices(170 + n), devices(171 + n)
        for turn in range(3 * LIMIT):
            method = "jukeboxControl" if turn % 2 else "ping"
            assert attacker.refused(method, SERVICE, f"guess-{turn}", action="status")
        assert attacker.refused("ping", SERVICE, ADMIN_PASSWORD)  # the attacker is locked out
        # Navidrome's login page, flooded from that address too.
        guess = {"username": SERVICE, "password": "x"}
        statuses = [
            attacker.http.post("/auth/login", json=guess).status_code for _ in range(2 * LIMIT)
        ]
        assert statuses[:LIMIT] == [401] * LIMIT and statuses[-1] == 429

        navidrome = world.services.navidrome
        assert world.server.call(lambda: navidrome.subsonic("ping"))["status"] == "ok"
        navidrome._jwt = None  # a new login of the service account to the native API
        assert world.server.call(navidrome.config) is not None
        # A commit's writes, scan and verification (all as the service account) go through.
        release = catalog_release(f"limiter-{n}", f"Limiter {n}", "Locked Out", 2)
        assert len(world.materialize(release).created) == 2
        # ... and so does the admin signing in from somewhere else.
        assert elsewhere.works(SERVICE, ADMIN_PASSWORD)
        right = {"username": SERVICE, "password": ADMIN_PASSWORD}
        assert elsewhere.http.post("/auth/login", json=right).status_code == 200

    in_one_window(scenario)


def test_the_dashboards_sign_in_counts_each_client_at_navidrome(
    world: DeliveryWorld, devices: Devices
) -> None:
    """The dashboard signs in with Navidrome's login, whose limit counts per address: one
    client's failed sign-ins fill that client's count at Navidrome, not Shijhon's, and
    leave another client's alone."""

    def scenario(n: int) -> None:
        guessing, admin = Browser(world.server.base_url), Browser(world.server.base_url)
        guessing.http.headers["X-Forwarded-For"] = f"203.0.113.{200 + n}"
        admin.http.headers["X-Forwarded-For"] = f"203.0.113.{201 + n}"
        try:
            for _ in range(LIMIT):  # as many as Shijhon's own limit lets through
                wrong = guessing.sign_in(SERVICE, "wrong")
                assert wrong.status_code == 400 and "Wrong username or password" in wrong.text
            # They were that client's logins for Navidrome: its own login is now refused there.
            right = {"username": SERVICE, "password": ADMIN_PASSWORD}
            assert devices(200 + n).http.post("/auth/login", json=right).status_code == 429
            assert admin.sign_in(SERVICE, ADMIN_PASSWORD).status_code == 303  # signed in
        finally:
            guessing.close()
            admin.close()

    in_one_window(scenario)
