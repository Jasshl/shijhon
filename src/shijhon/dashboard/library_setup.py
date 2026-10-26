"""One-time, administrator-reviewed conversion of existing original music."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from shijhon.access import AccessDenied, AccessService, AccessUnavailable, Principal
from shijhon.collection.contracts import Actor, CollectionError
from shijhon.collection.origins import Origins
from shijhon.dashboard.auth import Session
from shijhon.dashboard.collection import read_form
from shijhon.navidrome.client import NavidromeError, NavidromeService

if TYPE_CHECKING:
    from shijhon.dashboard.app import Dashboard


class SetupDenied(CollectionError):
    def __init__(self) -> None:
        super().__init__("This setup requires a current administrator with music-library access.")


class SetupUnavailable(CollectionError):
    def __init__(self) -> None:
        super().__init__("Existing accounts could not be verified. Try again later.")


@dataclass(frozen=True)
class Account:
    user_id: str
    username: str
    library_ids: frozenset[str]


def _identity(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict) or type(value.get("isAdmin")) is not bool:
        raise SetupUnavailable
    ident, name = value.get("id"), value.get("userName")
    if any(
        not isinstance(v, str) or not v or len(v) > 300 or not v.isprintable() or v != v.strip()
        for v in (ident, name)
    ):
        raise SetupUnavailable
    assert isinstance(ident, str) and isinstance(name, str)
    return ident, name


class SetupAccess:
    """Native administration metadata, always behind this session's fresh authority.

    Service credentials are used only for native administration. Song enumeration
    remains the collection service's caller-authorized structural-index operation.
    No raw native user payload is retained or returned to the page.
    """

    def __init__(
        self, navidrome: NavidromeService, session: Session, service_username: str
    ) -> None:
        self.navidrome, self.session = navidrome, session
        self.service_username = service_username.casefold()
        self.access = AccessService(navidrome)
        self.deadline = time.monotonic() + 60

    def _budget(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SetupUnavailable
        return min(5, remaining)

    async def authorize_setup(self, actor_id: str) -> Actor:
        if actor_id != self.session.user_id or not actor_id:
            raise SetupDenied
        try:
            with anyio.fail_after(self._budget()):
                principal = await self.access.personal(
                    self.session.username, expected_user_id=self.session.user_id
                )
        except AccessDenied:
            raise SetupDenied from None
        except (AccessUnavailable, TimeoutError):
            raise SetupUnavailable from None
        if not principal.is_admin or principal.username.casefold() == self.service_username:
            raise SetupDenied
        return self._actor(principal)

    @staticmethod
    def _actor(principal: Principal) -> Actor:
        return Actor(principal.user_id, frozenset(str(i) for i in principal.library_ids))

    async def _native(self, path: str, **kwargs: Any) -> Any:
        await self.authorize_setup(self.session.user_id)
        try:
            with anyio.fail_after(self._budget()):
                return await self.navidrome.native_json("GET", path, **kwargs)
        except (NavidromeError, TimeoutError, ValueError, TypeError):
            raise SetupUnavailable from None

    async def _account(self, ident: str, name: str) -> Account:
        await self.authorize_setup(self.session.user_id)
        if name.casefold() == self.service_username:
            raise SetupDenied
        try:
            with anyio.fail_after(self._budget()):
                principal = await self.access.verify(name, expected_user_id=ident)
        except (AccessDenied, AccessUnavailable, TimeoutError):
            raise SetupUnavailable from None
        return Account(ident, principal.username, self._actor(principal).library_ids)

    async def authorize(self, owner_id: str) -> Actor:
        if not owner_id or len(owner_id) > 300:
            raise SetupDenied
        user = await self._native("user/" + quote(owner_id, safe=""))
        ident, name = _identity(user)
        if ident != owner_id:
            raise SetupUnavailable
        account = await self._account(ident, name)
        return Actor(account.user_id, account.library_ids)

    async def accounts(self) -> list[Account]:
        """Bounded native roster, including people who have never used Shijhon."""
        people: list[tuple[str, str]] = []
        ids: set[str] = set()
        names: set[str] = set()
        try:
            with anyio.fail_after(30):
                for offset in range(0, 10_001, 500):
                    page = await self._native(
                        "user",
                        params={
                            "_start": offset,
                            "_end": offset + 500,
                            "_sort": "id",
                            "_order": "ASC",
                        },
                    )
                    if not isinstance(page, list) or len(page) > 500:
                        raise SetupUnavailable
                    for value in page:
                        ident, name = _identity(value)
                        if ident in ids or name.casefold() in names or len(ids) >= 10_000:
                            raise SetupUnavailable
                        ids.add(ident)
                        names.add(name.casefold())
                        if name.casefold() != self.service_username:
                            people.append((ident, name))
                    if len(page) < 500:
                        break
                accounts = [await self._account(ident, name) for ident, name in people]
        except TimeoutError:
            raise SetupUnavailable from None
        library = str(self.navidrome.library_id)
        eligible = [a for a in accounts if library in a.library_ids]
        if not any(a.user_id == self.session.user_id for a in eligible):
            raise SetupUnavailable
        return sorted(eligible, key=lambda a: (a.username.casefold(), a.user_id))


class Pages:
    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard

    def routes(self) -> list[Route]:
        return [
            Route(
                "/collection/setup",
                self.dashboard._page(self.index, form_reader=read_form),
                methods=["GET", "POST"],
            )
        ]

    async def index(self, request: Request, session: Session) -> Response:
        dashboard = self.dashboard
        services = dashboard.services
        assert services is not None and dashboard.store is not None
        store = dashboard.store
        access = SetupAccess(services.navidrome, session, dashboard.host.configured.navidrome.user)
        library = str(services.navidrome.library_id)
        origins = Origins(store)
        error, status = "", 200
        review: dict[str, Any] | None = None
        accounts: list[Account] = []
        chosen = session.user_id
        policy = None
        state = None
        counts: dict[str, Any] = {}
        try:
            await access.authorize_setup(session.user_id)
            policy = await store.fetchone(
                "SELECT mode,revision FROM collection_library_policy WHERE library_id=?", [library]
            )
            state = await store.fetchone(
                "SELECT generation,updated_at FROM collection_index WHERE library_id=?", [library]
            )
            row = await store.fetchone(
                "SELECT count(*) AS songs,coalesce(sum(original),0) AS originals"
                " FROM collection_tracks WHERE library_id=? AND present=1",
                [library],
            )
            counts = dict(row) if row else {}
            form = request.state.form if request.method == "POST" else {}
            # Replaying an approved form still goes through the original actor/review
            # and fresh account checks. A new review cannot reassign a personal policy.
            if form.get("action") == "approve":
                revision = form.get("revision", "")
                if not revision.isascii() or not revision.isdigit() or len(revision) > 12:
                    raise CollectionError("Reload setup and review the original music again.")
                await origins.apply_legacy(
                    form.get("setup_id", "")[:100],
                    actor_id=session.user_id,
                    expected_revision=int(form["revision"]),
                    access=access,
                )
                return dashboard._see("/collection/setup")
            if policy and policy["mode"] == "personal":
                if form:
                    raise CollectionError("Personal music libraries are already set up.")
            else:
                accounts = await access.accounts()
                if form:
                    if form.get("action") != "review" or not policy:
                        raise CollectionError("Reload setup and review the original music again.")
                    chosen = form.get("legacy_owner_id", "")
                    if chosen not in {a.user_id for a in accounts}:
                        raise CollectionError("Choose a currently authorized original music owner.")
                    if not state or not state["generation"] or state["updated_at"] <= 0:
                        raise CollectionError("Prepare the music index before reviewing setup.")
                    ident = await origins.review_legacy(
                        actor_id=session.user_id,
                        library_id=library,
                        legacy_owner_id=chosen,
                        existing_owner_ids=[a.user_id for a in accounts],
                        access=access,
                    )
                    row = await store.fetchone(
                        "SELECT s.id,s.expected_revision,"
                        "(SELECT count(*) FROM collection_original_setup_tracks t"
                        " WHERE t.setup_id=s.id) AS originals,"
                        "(SELECT count(*) FROM collection_original_setup_accounts a"
                        " WHERE a.setup_id=s.id AND a.owner_id<>s.legacy_owner_id) AS others,"
                        "(SELECT count(*) FROM collection_original_setup_profiles p"
                        " WHERE p.setup_id=s.id AND p.owner_id<>s.legacy_owner_id) AS profiles"
                        " FROM collection_original_setups s WHERE s.id=? AND s.actor_id=?",
                        [ident, session.user_id],
                    )
                    assert row is not None
                    review = dict(row)
        except SetupDenied as exc:
            error, status = str(exc), 403
            policy, state, counts, accounts, review = None, None, {}, [], None
        except SetupUnavailable as exc:
            error, status = str(exc), 503
            policy, state, counts, accounts, review = None, None, {}, [], None
        except CollectionError as exc:
            error, status = str(exc), 409
        return await dashboard._render(
            "library_setup.html",
            session=session,
            current="collection",
            status_code=status,
            error=error,
            policy=policy,
            counts=counts,
            accounts=accounts,
            chosen=chosen,
            ready=bool(state and state["generation"] and state["updated_at"] > 0),
            review=review,
        )
